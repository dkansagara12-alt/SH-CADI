"""Final demonstration for ONE system (run in its own process):
build N records -> inject k faults -> detect -> localize -> recover -> repair -> verify.

    python -m sh_cadi.benchmark.final_demo shcadi 1000000 100
"""
import gc
import json
import sys
import time

import psutil

from sh_cadi.benchmark.systems import (ConventionalIndex, SHCADIIndex, TargetedConventionalIndex,
                                       build, make_record)
from sh_cadi.fault_injection.injector import FaultInjector

CLS = {"shcadi": SHCADIIndex, "conventional": ConventionalIndex, "conventional_targeted": TargetedConventionalIndex}
TITLE = {"shcadi": "SH-CADI (self-healing, localized recovery)",
         "conventional": "CONVENTIONAL (hash table + Merkle + full rebuild)",
         "conventional_targeted": "CONVENTIONAL + targeted repair (stronger baseline)"}


def ms(x):
    return f"{x * 1000:,.1f} ms" if x < 1 else f"{x:,.2f} s"


def run(system, n, k, seed=2024):
    gc.disable()
    print(f"\n--- {TITLE[system]} : {n:,} metadata records, {k} corrupted ---", flush=True)
    base = psutil.Process().memory_info().rss
    t = time.perf_counter()
    s = build(CLS[system], n)
    print(f"  built index in {ms(time.perf_counter() - t)}; memory added {(psutil.Process().memory_info().rss - base) / 1e6:,.0f} MB", flush=True)
    inj = FaultInjector(s.ht, getattr(s, "tree", None), seed=seed)
    inj.inject_random(k, kinds=list(inj.METADATA_KINDS))
    kinds = {}
    for f in inj.log:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    print(f"  injected {len(inj.log)} faults: {kinds}", flush=True)
    res = {"system": system, "n": n, "k": k}

    if system == "shcadi":
        rep = s.shi.heal()
        T, loc = rep["timings"], rep["localization"]
        print(f"  [1 Detect  ] integrity violation detected                  {ms(T['detect_s'])}  (reads all {n:,} records)")
        print(f"  [2 Localize] {loc['nodes_compared']} Merkle nodes compared -> {len(loc['flagged_buckets'])} damaged buckets of "
              f"{s.tree.num_buckets:,}; {loc['records_examined']} of {n:,} records examined   {ms(T['localize_s'])}")
        print(f"  [3 Recover ] {rep['journal_shards_read']} journal shards read ({rep['journal_entries_read']} lines); "
              f"sources {rep['sources']}   {ms(T['recover_s'])}")
        print(f"  [4 Repair  ] reconstructed {rep['records_reconstructed']:,} records, removed {rep['records_removed']} spurious   {ms(T['repair_s'])}")
        print(f"  [5 Verify  ] local checks pass: {rep['verified_local']}; stored root == anchor: {rep['root_restored']}   {ms(T['verify_s'])}")
        ok = bool(rep["success"] and not s.detect())
        print(f"  independent full audit after repair: {'INTACT' if ok else 'FAILED'}")
        print(f"  recovery locality = {rep['records_reconstructed']:,} / {n:,} = {rep['recovery_locality']:.2e}")
        rec_phase = T["localize_s"] + T["recover_s"] + T["repair_s"] + T["verify_s"]
        t = time.perf_counter()
        for i in range(2000):
            s.insert(*make_record(i, "after"))
        print(f"  system keeps operating: 2,000 new inserts in {ms(time.perf_counter() - t)}; audit intact: {not s.detect()}")
        res.update(detect_s=T["detect_s"], recovery_phase_s=rec_phase, total_s=T["detect_s"] + rec_phase,
                   reconstructed=rep["records_reconstructed"], locality=rep["recovery_locality"], success=ok)
    else:
        found, td = _t(s.detect)
        print(f"  [1 Detect  ] integrity violation detected: {found}              {ms(td)}  (rebuilds the whole Merkle tree)")
        aff, tl = _t(s.localize)
        print(f"  [2 Localize] full scan compares all {n:,} leaves -> {len(aff)} damaged records   {ms(tl)}")
        nrec, tr = _t(s.recover)
        what = "every record rebuilt from the full journal" if system == "conventional" else "only the damaged records re-read from the journal"
        print(f"  [3-4 Recover+Repair] {nrec:,} records reconstructed ({what})   {ms(tr)}")
        ok, tv = _t(s.verify)
        print(f"  [5 Verify  ] full Merkle rebuild matches baseline root: {ok}   {ms(tv)}")
        print(f"  recovery locality = {nrec:,} / {len(s.ht):,} = {nrec / len(s.ht):.2e}")
        res.update(detect_s=td, recovery_phase_s=tl + tr + tv, total_s=td + tl + tr + tv,
                   reconstructed=nrec, locality=nrec / len(s.ht), success=bool(ok))
    print("RESULT " + json.dumps(res), flush=True)


def _t(fn):
    t = time.perf_counter()
    r = fn()
    return r, time.perf_counter() - t


if __name__ == "__main__":
    run(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
