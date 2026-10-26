"""Turn results.json into plots + RESULTS.md.  Every number in the report comes from the JSON."""
import json
import os
import sys

from sh_cadi.visualization.plots import make_all

SYS = ("conventional", "conventional_targeted", "shcadi")
NAME = {"conventional": "conventional (full rebuild)", "conventional_targeted": "conventional + targeted repair", "shcadi": "SH-CADI"}


def ms(x):
    return f"{x * 1000:,.1f}" if x < 1 else f"{x * 1000:,.0f}"


def sec(x):
    return f"{x:.3f}" if x < 10 else f"{x:.1f}"


def rng(m):
    return f"{ms(m['median'])} ({ms(m['min'])}-{ms(m['max'])})"


def build(path="benchmark_results/results.json", outdir=None):
    data = json.load(open(path))
    outdir = outdir or os.path.dirname(path)
    plots = make_all(data, os.path.join(outdir, "plots"))
    runs = {(r["system"], r["n"]): r for r in data["runs"] if "failed" not in r}
    failed = [r for r in data["runs"] if "failed" in r]
    sizes = sorted({n for (_, n) in runs})
    env, no = data["environment"], data["normal_ops"]
    o = []
    w = o.append
    w("# SH-CADI evaluation results\n")
    w("All numbers below were measured by this code on the machine below and are copied from `results.json`; nothing is estimated.\n")
    w(f"**Environment:** Python {env['python']}, {env['cpus']} CPU, {env['ram_gb']} GB RAM, {env['platform']}. "
      f"Each (system, size) ran in its own fresh process. Synthetic records (SHA-256 keys, 4 KB nominal chunks), "
      f"in-memory journal, no disk or network I/O modelled.\n")
    w("**Systems.** *Conventional* = chained hash table + the same sharded journal + Phase-1 sorted-leaf Merkle tree rebuilt on demand; "
      "recovery = replay the whole journal and rebuild. *SH-CADI* = same hash table + same journal + incremental bucketed Merkle tree + localized heal. "
      "Both get identical recovery information.\n")

    w("## 1. Normal operation (cost of self-healing)\n")
    w(f"Dedup upload pipeline, {no['chunks']:,} chunks of 4 KB ({no['dup_ratio']:.0%} duplicates), in-memory chunk store, median of 5.\n")
    w("| config | chunks/s | MB/s | time vs hash-table-only | time range (s) |\n|---|---:|---:|---:|---|")
    for k, lab in (("plain", "hash table only"), ("conventional", "conventional (+journal)"), ("shcadi", "SH-CADI (+journal +Merkle +verify-before-write)")):
        c = no["configs"][k]
        w(f"| {lab} | {c['chunks_per_s']:,.0f} | {c['mb_per_s']:.1f} | {c['overhead_vs_plain']:.2f}x | {c['min_s']:.3f}-{c['max_s']:.3f} |")
    w("")

    w("## 2. Per-size basics\n")
    w("| records | system | build (s) | memory added (MB) | journal (MB) | lookup hit (us) | lookup miss (us) | authenticated lookup (us) | insert (us) | clean full verify (s) | peak RSS of whole run (MB) |\n|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for n in sizes:
        for s in SYS:
            r = runs.get((s, n))
            if r:
                w(f"| {n:,} | {NAME[s]} | {r['build_s']:.1f} | {r['memory_mb']:,.0f} | {r['journal_mb']:.1f} | {r['lookup_present_us']:.2f} | "
                  f"{r['lookup_absent_us']:.2f} | {r.get('verified_lookup_us', float('nan')):.1f} | {r['insert_us']:.1f} | {r['verify_clean_s']:.2f} | {r['peak_rss_mb']:,.0f} |")
    w("")
    for r in failed:
        w(f"> **{r['system']} n={r['n']:,} FAILED:** {r['failed']}\n")

    w("## 3. Fault sweep (k corrupted records, metadata faults of 5 kinds, seeded)\n")
    w("Times in ms, median (min-max) over repetitions. *Recovery phase* = localize + recover + repair + verify; *total* adds detection. "
      "Detection reads every record in both systems.\n")
    w("| records | k | system | detect | localize | recovery phase | total | records reconstructed | spurious removed | recovery locality | success | localized exactly |\n|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for n in sizes:
        for k in sorted({int(x[2:]) for r in runs.values() if r["n"] == n for x in r["sweeps"] if x.startswith("k=")}):
            for s in SYS:
                r = runs.get((s, n))
                if not r or f"k={k}" not in r["sweeps"]:
                    continue
                v = r["sweeps"][f"k={k}"]
                w(f"| {n:,} | {k} | {NAME[s]} | {rng(v['detect_s'])} | {rng(v['localize_s'])} | {rng(v['recovery_phase_s'])} | {rng(v['total_s'])} | "
                  f"{v['reconstructed']['median']:,.0f} | {v['removed']['median']:,.0f} | {v['locality']['median']:.2e} | {v['success']:.0%} | {v['localized_exactly']:.0%} |")
    w("")

    w("## 4. Corruption-rate sweep\n")
    w("| records | rate | k | system | recovery phase (ms) | total (ms) | records reconstructed | recovery locality | success |\n|---:|---:|---:|---|---:|---:|---:|---:|---:|")
    for n in sizes:
        rates = sorted({float(x[5:]) for r in runs.values() if r["n"] == n for x in r["sweeps"] if x.startswith("rate=")})
        for q in rates:
            for s in SYS:
                r = runs.get((s, n))
                if not r or f"rate={q}" not in r["sweeps"]:
                    continue
                v = r["sweeps"][f"rate={q}"]
                w(f"| {n:,} | {q:.1%} | {v['k_abs']:,} | {NAME[s]} | {ms(v['recovery_phase_s']['median'])} | {ms(v['total_s']['median'])} | "
                  f"{v['reconstructed']['median']:,.0f} | {v['locality']['median']:.4f} | {v['success']:.0%} |")
    w("")

    w("## 5. Robustness: 10 metadata faults + one journal shard wiped (shard holds no faulty record)\n")
    w("| records | system | recovery succeeded | pristine records lost by recovery | records in wiped shard |\n|---:|---|---:|---:|---:|")
    for n in sizes:
        for s in SYS:
            r = runs.get((s, n))
            if r:
                sl = r["shard_loss"]
                w(f"| {n:,} | {NAME[s]} | {sl['success']} | {sl['records_lost_by_recovery']} | {sl['wiped_shard_records']} |")
    w("")

    w("## 6. Read-path detection (SH-CADI only)\n")
    w("Median latency of an authenticated read that finds a corrupted record and repairs just that record:\n")
    w("| records | latency (us) | index intact afterwards |\n|---:|---:|---|")
    for n in sizes:
        r = runs.get(("shcadi", n))
        if r:
            w(f"| {n:,} | {r['readpath_detect_repair_us']:.0f} | {r['readpath_index_intact_after']} |")
    w("")

    w("## 7. Data-driven observations\n")
    w("Ratios are baseline time / SH-CADI time (>1 means SH-CADI is faster). *Recovery phase* excludes detection; *end-to-end* includes it.\n")
    for n in sizes:
        a, b, t = runs.get(("shcadi", n)), runs.get(("conventional", n)), runs.get(("conventional_targeted", n))
        if not (a and b):
            continue
        for k in (1, 100):
            ka = a["sweeps"].get(f"k={k}")
            if not ka:
                continue
            for base, nm in ((b, "full rebuild"), (t, "targeted repair")):
                kb = base["sweeps"].get(f"k={k}") if base else None
                if kb:
                    ra, rb = ka["recovery_phase_s"]["median"], kb["recovery_phase_s"]["median"]
                    ta, tb = ka["total_s"]["median"], kb["total_s"]["median"]
                    w(f"* **{n:,} records, k={k}, vs {nm}:** recovery phase {ms(ra)} ms vs {ms(rb)} ms ({rb / ra:,.1f}x); "
                      f"end-to-end {sec(ta)} s vs {sec(tb)} s ({tb / ta:.1f}x).")
        w(f"* **{n:,} records, memory after build:** SH-CADI {a['memory_mb']:,.0f} MB vs conventional {b['memory_mb']:,.0f} MB ({a['memory_mb'] / b['memory_mb']:.2f}x); "
          f"insert {a['insert_us']:.0f} us vs {b['insert_us']:.0f} us ({a['insert_us'] / b['insert_us']:.1f}x slower); "
          f"clean full verification {a['verify_clean_s']:.2f} s vs {b['verify_clean_s']:.2f} s.")
    for n in sizes:
        a, b, t = runs.get(("shcadi", n)), runs.get(("conventional", n)), runs.get(("conventional_targeted", n))
        rates = sorted(float(x[5:]) for x in (a or {}).get("sweeps", {}) if x.startswith("rate="))
        if a and b and rates:
            q = rates[-1]
            va, vb = a["sweeps"][f"rate={q}"], b["sweeps"][f"rate={q}"]
            ra = va["recovery_phase_s"]["median"]
            msg = (f"* **Highest corruption rate tested ({q:.0%} of {n:,} records), locality {va['locality']['median']:.2f}:** SH-CADI recovery phase "
                   f"{ms(ra)} ms vs {ms(vb['recovery_phase_s']['median'])} ms for full rebuild ({vb['recovery_phase_s']['median'] / ra:.1f}x)")
            if t:
                vt = t["sweeps"][f"rate={q}"]
                rt = vt["recovery_phase_s"]["median"]
                msg += f" and {ms(rt)} ms for targeted repair ({rt / ra:.2f}x" + (" -- no meaningful advantage at this rate)." if rt / ra < 1.15 else ").")
            w(msg + " The advantage shrinks as the corruption rate grows.")
    w(f"* **Normal operation:** SH-CADI sustains {no['configs']['shcadi']['chunks_per_s']:,.0f} chunks/s vs "
      f"{no['configs']['conventional']['chunks_per_s']:,.0f} (journal only) and {no['configs']['plain']['chunks_per_s']:,.0f} (hash table only).")
    w("* **Shard-loss scenario (section 5):** the baseline that replays the whole journal loses the records whose shard was wiped, while any system "
      "that repairs only the damaged records survives it, *including the conventional baseline with targeted repair*. That robustness therefore comes from "
      "**localized recovery**, not from the Merkle tree itself.")
    w("* **What the Merkle tree buys over targeted repair:** localization and verification without extra O(n) passes (compare the localize and total columns), "
      "plus O(log B) authenticated reads. Detection remains O(n) in all systems.")
    w("")
    w("## 8. Limitations (read before citing any number)\n")
    w("* Single machine, single-threaded pure Python; absolute times are not representative of a C/Rust system. Ratios are more meaningful than absolutes.")
    w("* Repetitions are few (5 at 10k, 3 at 100k, **1 at 1M**); the 1M row has no variance estimate. Rate-sweep points are single runs.")
    w("* Journal is in memory: disk I/O, network latency and cache effects are not modelled. Real recovery would add I/O to both systems, in different amounts.")
    w("* Detection costs O(n) in both systems (every record is hashed once); SH-CADI's advantage is in localization and recovery, so end-to-end gains are smaller than recovery-phase gains.")
    w("* SH-CADI pays for its design in normal operation (slower inserts, higher memory, authenticated reads); see sections 1-2.")
    w("* Baselines are design choices. 'Full rebuild' is the baseline the project brief defines; 'targeted repair' is a stronger one I added after noticing the first was easy to beat. "
      "Both use Phase-1's Merkle tree whose localization step re-hashes every record; a baseline that reused the detection pass, or a checkpointed index, could narrow the gap further. These were not tested.")
    w("* Faults are synthetic and uniformly random; correlated damage (e.g. a whole region) is not modelled except for the single wiped-shard scenario.")
    w("* Threat model: accidental faults with a trusted anchor root. No adversarial guarantees are claimed.")
    w("")
    w("## Plots\n")
    for p in plots:
        w(f"![{p}](plots/{p})\n")
    text = "\n".join(o)
    with open(os.path.join(outdir, "RESULTS.md"), "w") as f:
        f.write(text)
    return text


if __name__ == "__main__":
    build(*(sys.argv[1:2] or ["benchmark_results/results.json"]))
    print("wrote RESULTS.md and plots")
