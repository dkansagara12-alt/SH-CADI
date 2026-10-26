"""SH-CADI full project demonstration -- one run covers the whole system end to end:

    1. Content-addressable deduplication       (upload, dedup, reconstruct)
    2. Authenticated index                     (bucketed Merkle tree, trusted root)
    3. Self-healing of metadata                (inject -> detect -> localize -> recover -> repair -> verify)
    4. Mixed faults + damaged recovery data    (digest-checked repair, never installs bad data)
    5. Automatic triggers                      (verify-before-write, verified reads)
    6. Chunk-data recovery                     (Reed-Solomon RS(4,2), node failure, beyond-tolerance reporting)
    7. Evaluation at scale                     (SH-CADI vs conventional baselines, N records, k faults)

Run from the project root:
    python -m sh_cadi.main                 # full demo, scale section with 100,000 records
    python -m sh_cadi.main 1000000         # scale section with 1,000,000 records (~2.2 GB RAM)
    python -m sh_cadi.main --quick         # skip the scale section (about 5 seconds)

All numbers are measured live.  Working files go to a temporary folder that is deleted afterwards.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

from sh_cadi.dedup_system import ContentAddressableStorage
from sh_cadi.fault_injection.injector import FaultInjector
from sh_cadi.merkle.bucket_merkle import BucketMerkleTree
from sh_cadi.merkle.record_digest import record_digest
from sh_cadi.recovery.healer import SelfHealingIndex
from sh_cadi.recovery.journal import MetadataJournal
from sh_cadi.storage.protected_store import ProtectedChunkStore
from sh_cadi.recovery.erasure import ReedSolomon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(ROOT, "sample_files")
CHUNK, DEPTH = 64, 6
CHECKS = []                      # (description, passed) -- printed as the final scorecard


def banner(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72, flush=True)


def check(desc, ok):
    CHECKS.append((desc, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")


def short(h):
    return h[:12] + "..."


def snapshot(ht):
    return {h: record_digest(h, e) for h, e in ht.items()}


def sample_files():
    """Input files for the demo; created once if the folder is missing."""
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    a = ("The quick brown fox jumps over the lazy dog. " * 20).encode()
    b = (("The quick brown fox jumps over the lazy dog. " * 8)
         + ("Completely different content lives here for File B only. " * 10)).encode()
    paths = []
    for name, data in (("FileA.txt", a), ("FileB.txt", b)):
        p = os.path.join(SAMPLE_DIR, name)
        if not os.path.exists(p):
            with open(p, "wb") as f:
                f.write(data)
        paths.append(p)
    return paths


def same_file(cas, src, out):
    cas.reconstruct_file(src, out)
    with open(src, "rb") as a, open(out, "rb") as b:
        return a.read() == b.read()


def build_system(work, files):
    """Dedup store + self-healing index (Merkle tree + sharded journal), FileA, FileB, FileA uploaded."""
    for d in ("store", "journal"):
        shutil.rmtree(os.path.join(work, d), ignore_errors=True)
    cas = ContentAddressableStorage(chunk_size=CHUNK, storage_root=os.path.join(work, "store"))
    shi = SelfHealingIndex(cas.hash_table, BucketMerkleTree(DEPTH),
                           MetadataJournal(DEPTH, os.path.join(work, "journal"))).attach(cas)
    reports = [cas.upload_file(p) for p in files + [files[0]]]
    return cas, shi, reports


def print_heal(r):
    T, loc = r["timings"], r["localization"]
    print(f"  [1 Detect  ] integrity violation detected                 {T['detect_s']*1000:8.2f} ms")
    print(f"  [2 Localize] damaged buckets {r['flagged_buckets']}; {loc['nodes_compared']} nodes compared, "
          f"{loc['records_examined']} of {loc['total_records']} records examined   {T['localize_s']*1000:.3f} ms")
    print(f"  [3 Recover ] sources {r['sources'] or '-'}; journal shards read {r['journal_shards_read']}"
          f"   {T['recover_s']*1000:.3f} ms")
    print(f"  [4 Repair  ] reconstructed {r['records_reconstructed']}, removed {r['records_removed']}, "
          f"leaf-only repairs {r['leaf_only_repairs']}   {T['repair_s']*1000:.3f} ms")
    print(f"  [5 Verify  ] local={r['verified_local']} full audit={r['verified_full']} "
          f"root restored={r['root_restored']}   {T['verify_s']*1000:.2f} ms")
    print(f"  recovery locality = {r['records_reconstructed']}/{r['total_records']} = {r['recovery_locality']:.4f}"
          f"   unrecoverable: {r['unrecoverable'] or 'none'}")


def section_dedup(work, files):
    banner("1. Content-addressable deduplication")
    cas, shi, reports = build_system(work, files)
    for label, r in zip(("File A", "File B", "File A again"), reports):
        print(f"  upload {label:13s}: {r['total_chunks']:3d} chunks, {r['unique_chunks']:3d} new, "
              f"{r['duplicate_chunks']:3d} duplicates, {r['bytes_saved']:5d} bytes saved")
    st = cas.global_stats()
    print(f"  total: {st['unique_chunks_stored']} unique chunks stored, "
          f"{st['total_logical_bytes']} logical bytes, {st['total_saved_bytes']} saved by dedup")
    print("  sample index entries (hash -> chunk id, ref_count):")
    for h, e in list(cas.hash_table.items())[:3]:
        print(f"    {short(h)} -> {e['chunk_id']:<28} ref_count={e['ref_count']}")
    check("duplicate upload of File A stores no new chunks", reports[2]["unique_chunks"] == 0)
    check("File A reconstructs byte-for-byte from chunks",
          same_file(cas, files[0], os.path.join(work, "FileA_out.txt")))


def section_index(work, files):
    banner("2. Authenticated index: bucketed Merkle tree + trusted root")
    cas, shi, _ = build_system(work, files)
    print(f"  {len(cas.hash_table)} records in {shi.tree.num_buckets} hash-prefix buckets (tree depth {shi.tree.depth})")
    print(f"  trusted (anchor) root: {shi.tree.trusted_root[:32]}...")
    print(f"  journal: {len(shi.journal.shards())} shard files, {shi.journal.line_count()} lines, "
          f"{shi.journal.size_bytes()} bytes (one shard per Merkle bucket)")
    check("clean system audits as intact", shi.auditor.detect()["intact"])


def section_heal(work, files):
    banner("3. Self-healing a corrupted record:  H -> Chunk_B  becomes  H -> Chunk_X")
    cas, shi, _ = build_system(work, files)
    truth = snapshot(cas.hash_table)
    inj = FaultInjector(cas.hash_table, shi.tree, cas.storage, seed=42)
    h = list(cas.hash_table.items())[len(cas.hash_table) // 2][0]
    print(f"  before  : {short(h)} -> {cas.hash_table.search(h)['chunk_id']}")
    inj.change_chunk_ref(h, "Chunk_X")
    print(f"  injected: {short(h)} -> {cas.hash_table.search(h)['chunk_id']}")
    r = shi.heal(verify_full=True)
    print_heal(r)
    print(f"  after   : {short(h)} -> {cas.hash_table.search(h)['chunk_id']}")
    check("only the damaged record was localized", r["localization"]["affected_record_hashes"] == [h])
    check("index identical to pre-fault state", snapshot(cas.hash_table) == truth)


def section_mixed(work, files):
    banner("4. Mixed faults: 4 metadata faults + 2 Merkle leaves + a damaged journal line")
    cas, shi, _ = build_system(work, files)
    truth = snapshot(cas.hash_table)
    inj = FaultInjector(cas.hash_table, shi.tree, cas.storage, seed=11)
    faults = inj.inject_random(4, merkle_leaves=2)
    victim = faults[0]["chunk_hash"]
    if faults[0]["kind"] != "delete_entry":
        inj.modify_ref_count(victim, 3)
    inj.corrupt_journal_record(shi.journal, victim)     # newest recovery line for one victim is unusable
    for f in inj.log:
        print(f"  injected {f['kind']:18s} bucket {f['bucket']:<3} {short(f['chunk_hash']) if f['chunk_hash'] else ''}")
    r = shi.heal(verify_full=True)
    print_heal(r)
    check("all faults repaired (bad journal line rejected by digest check)", r["success"])
    check("index identical to pre-fault state", snapshot(cas.hash_table) == truth)


def section_triggers(work, files):
    banner("5. Automatic triggers: verify-before-write and verified reads")
    cas, shi, _ = build_system(work, files)
    inj = FaultInjector(cas.hash_table, shi.tree, cas.storage, seed=5)
    hs = [x for x, _ in cas.hash_table.items()]
    inj.modify_ref_count(hs[0], 99)
    inj.corrupt_merkle_leaf(shi.tree.bucket_of(hs[0]))
    print("  damaged one record's ref_count and its Merkle leaf, then uploaded File A again")
    cas.upload_file(files[0])
    c = Counter(x for rep in cas.upload_log for x in rep["manifest"])
    check("index intact after the write (healed before writing)", shi.auditor.detect()["intact"])
    check("ref_counts match the upload log (corrupt value not written back)",
          all(e["ref_count"] == c[k] for k, e in cas.hash_table.items()))
    inj.modify_location(hs[3])
    _, status = shi.verified_get(hs[3])
    print(f"  verified_get() on a damaged record -> {status}")
    check("system keeps serving correct files",
          same_file(cas, files[0], os.path.join(work, "FileA_out.txt")))


def section_chunks(work):
    banner("6. Chunk-data recovery: RS(4,2) erasure coding across 6 storage nodes")
    rng = random.Random(7)
    blocks = [bytes(rng.randrange(256) for _ in range(512)) for _ in range(24)]
    paths = []
    for name, parts in (("a.bin", blocks[:16]), ("b.bin", blocks[:8] + blocks[16:]), ("c.bin", blocks[4:20])):
        p = os.path.join(work, name)
        with open(p, "wb") as f:
            f.write(b"".join(parts))
        paths.append(p)
    store = ProtectedChunkStore(os.path.join(work, "nodes"), ReedSolomon(4, 2))
    cas = ContentAddressableStorage(chunk_size=512, storage=store)
    shi = SelfHealingIndex(cas.hash_table, BucketMerkleTree(DEPTH), MetadataJournal(DEPTH),
                           chunk_store=store).attach(cas)
    for p in paths:
        cas.upload_file(p)
    st = cas.global_stats()
    print(f"  {st['unique_chunks_stored']} unique chunks; {st['total_logical_bytes']} bytes uploaded, "
          f"{store.bytes_on_disk()} bytes on disk after dedup (each chunk = 4 data + 2 parity fragments)")
    inj = FaultInjector(cas.hash_table, shi.tree, store, seed=4)
    inj.inject_random(3, merkle_leaves=1)
    inj.inject_chunk_faults(5, per_chunk=1)
    inj.fail_node(3)
    print(f"  injected: {dict(Counter(f['kind'] for f in inj.log))}")
    rep = shi.heal_all(verify_full=True)
    m, c = rep["metadata"], rep["chunks"]
    print(f"  metadata: reconstructed {m['records_reconstructed']} record(s), success {m['success']}")
    print(f"  chunks  : repaired {c['chunks_repaired']}/{c['chunks_scanned']}, rewrote "
          f"{c['fragments_rewritten']} fragments only, success {c['success']}")
    check("metadata and chunk data both healed", rep["success"])
    check("every file reconstructs identically",
          all(same_file(cas, p, os.path.join(work, "out.bin")) for p in paths))
    h = list(cas.hash_table.items())[0][0]
    for i in (0, 1, 2):
        inj.delete_fragment(h, i)
    rep = shi.heal_all()
    print(f"  lost 3 fragments of one chunk (RS(4,2) tolerates 2): unrecoverable "
          f"{[x[:12] + '...' for x in rep['chunks']['unrecoverable']]}")
    check("beyond-tolerance loss is reported, not silently 'fixed'",
          not rep["success"] and h in rep["chunks"]["unrecoverable"])


def section_scale(n, k=100):
    banner(f"7. Evaluation at scale: {n:,} metadata records, {k} corrupted")
    print("  Each system runs in its own process so memory is not shared.", flush=True)
    results = []
    for system in ("shcadi", "conventional", "conventional_targeted"):
        p = subprocess.Popen([sys.executable, "-m", "sh_cadi.benchmark.final_demo", system, str(n), str(k)],
                             stdout=subprocess.PIPE, text=True, cwd=ROOT)
        for line in p.stdout:
            if line.startswith("RESULT "):
                results.append(json.loads(line[7:]))
            else:
                print(line, end="", flush=True)
        p.wait()
    if len(results) != 3:
        check("all three systems completed", False)
        return
    print(f"\n  {'system':24} {'recovery phase':>15} {'end-to-end':>11} {'reconstructed':>14} {'locality':>10} {'ok':>4}")
    for r in results:
        print(f"  {r['system']:24} {r['recovery_phase_s']:14.3f}s {r['total_s']:10.2f}s {r['reconstructed']:>14,} "
              f"{r['locality']:10.2e} {r['success']!s:>4}")
    a = results[0]
    for r in results[1:]:
        print(f"  SH-CADI vs {r['system']}: recovery phase {r['recovery_phase_s'] / a['recovery_phase_s']:,.1f}x faster, "
              f"end-to-end {r['total_s'] / a['total_s']:.1f}x faster")
    print("  (detection reads every record in all systems, so end-to-end gains are smaller than recovery-phase gains)")
    check("all three systems recovered correctly", all(r["success"] for r in results))
    check("SH-CADI reconstructs only damaged records", a["reconstructed"] < n)


def main(*args):
    quick = "--quick" in args
    nums = [a for a in args if a.isdigit()]
    n = int(nums[0]) if nums else 100_000
    print("=" * 72)
    print("SH-CADI: Self-Healing Content-Addressable Data Structure")
    print("         for Fault-Tolerant Cloud Storage Deduplication -- full demo")
    print("=" * 72)
    files = sample_files()
    work = tempfile.mkdtemp(prefix="shcadi_demo_")
    try:
        section_dedup(work, files)
        section_index(work, files)
        section_heal(work, files)
        section_mixed(work, files)
        section_triggers(work, files)
        section_chunks(work)
        if not quick:
            section_scale(n)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    banner("SCORECARD")
    for desc, ok in CHECKS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")
    passed = sum(ok for _, ok in CHECKS)
    print(f"\n  {passed}/{len(CHECKS)} checks passed")
    return passed == len(CHECKS)


if __name__ == "__main__":
    sys.exit(0 if main(*sys.argv[1:]) else 1)
