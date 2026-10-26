"""One (system, n) benchmark run in a FRESH process (isolated memory).  Emits one JSON object.

    python -m sh_cadi.benchmark.worker --system shcadi --n 100000 --reps 3 --ks 1,10,100 --rates 0.01,0.05

Order matters: build -> lookups (read-only) -> clean verify -> fault sweeps (each trial restores the
index) -> read-path -> journal-shard-loss scenario (destructive for the baseline) -> insert micro-benchmark.
"""
import argparse
import gc
import json
import random
import statistics
import sys
import time

import psutil

from sh_cadi.benchmark.systems import ConventionalIndex, SHCADIIndex, TargetedConventionalIndex, build, make_record
from sh_cadi.fault_injection.injector import FaultInjector


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def timed(fn):
    t = time.perf_counter()
    r = fn()
    return r, time.perf_counter() - t


# ------------------------------------------------------------------ fault trials
def trial_shcadi(s, k, seed):
    inj = FaultInjector(s.ht, s.tree, seed=seed)
    inj.inject_random(k, kinds=list(inj.METADATA_KINDS))
    rep = s.shi.heal()
    T = rep["timings"]
    loc = rep["localization"]
    rec_phase = T["localize_s"] + T["recover_s"] + T["repair_s"] + T["verify_s"]
    audit_ok = not s.detect()                                        # independent, untimed
    return {"k": k, "detect_s": T["detect_s"], "localize_s": T["localize_s"],
            "recover_s": T["recover_s"] + T["repair_s"], "verify_s": T["verify_s"],
            "recovery_phase_s": rec_phase, "total_s": T["detect_s"] + rec_phase,
            "reconstructed": rep["records_reconstructed"], "removed": rep["records_removed"],
            "locality": rep["recovery_locality"], "buckets_touched": rep["buckets_repaired"],
            "nodes_compared": loc["nodes_compared"], "records_examined": loc["records_examined"],
            "journal_entries_read": rep["journal_entries_read"],
            "localized_exactly": set(loc["affected_record_hashes"]) == inj.affected_hashes(),
            "success": bool(rep["success"] and audit_ok)}


def trial_conv(s, k, seed):
    inj = FaultInjector(s.ht, None, seed=seed)
    inj.inject_random(k, kinds=list(inj.METADATA_KINDS))
    found, t_det = timed(s.detect)
    affected, t_loc = timed(s.localize)
    n_rec, t_rec = timed(s.recover)
    ok, t_ver = timed(s.verify)
    n = len(s.ht)
    return {"k": k, "detect_s": t_det, "localize_s": t_loc, "recover_s": t_rec, "verify_s": t_ver,
            "recovery_phase_s": t_loc + t_rec + t_ver, "total_s": t_det + t_loc + t_rec + t_ver,
            "reconstructed": n_rec, "removed": 0, "locality": n_rec / n, "buckets_touched": None,
            "nodes_compared": None, "records_examined": n, "journal_entries_read": s.journal.line_count(),
            "localized_exactly": affected == inj.affected_hashes(), "success": bool(found and ok)}


# ------------------------------------------------------------------ scenarios
def shard_loss(s, shcadi, k, seed):
    """10 metadata faults + one journal shard wiped (a shard holding NO faulty record)."""
    pristine = {h for h, _ in s.ht.items()}                           # keys before any damage
    inj = FaultInjector(s.ht, s.tree if shcadi else None, seed=seed)
    inj.inject_random(k, kinds=list(inj.METADATA_KINDS))
    bad = {s.journal.shard_of(h) for h in inj.affected_hashes()}
    victim = next(b for b in sorted(s.journal.shards()) if b not in bad)
    in_shard = len(s.journal.read_shard(victim)["latest"])
    s.journal.wipe_shard(victim)
    if shcadi:
        rep = s.shi.heal()
        ok = bool(rep["success"] and not s.detect())
        reconstructed = rep["records_reconstructed"]
    else:
        s.detect(); s.localize()
        reconstructed = s.recover()
        ok = bool(s.verify())
    lost = sum(1 for h in pristine if s.ht.search(h) is None)         # pristine records absent afterwards
    return {"wiped_shard_records": in_shard, "success": ok, "records_lost_by_recovery": lost,
            "records_reconstructed": reconstructed}


def summarize(trials):
    out = {}
    for key in trials[0]:
        vals = [t[key] for t in trials]
        if isinstance(vals[0], bool):
            out[key] = sum(vals) / len(vals)
        elif isinstance(vals[0], (int, float)):
            out[key] = {"median": statistics.median(vals), "mean": statistics.mean(vals),
                        "min": min(vals), "max": max(vals),
                        "stdev": statistics.pstdev(vals) if len(vals) > 1 else 0.0}
    return out


def peak_rss_mb():
    """Peak resident memory of this process in MB (resource is Unix-only; psutil covers Windows)."""
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:
        mi = psutil.Process().memory_info()
        return getattr(mi, "peak_wset", mi.rss) / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["shcadi", "conventional", "conventional_targeted"], required=True)
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--ks", default="1,10,100")
    ap.add_argument("--rates", default="")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    gc.disable()
    shc = a.system == "shcadi"
    cls = {"shcadi": SHCADIIndex, "conventional": ConventionalIndex,
           "conventional_targeted": TargetedConventionalIndex}[a.system]
    proc = psutil.Process()
    base = proc.memory_info().rss
    res = {"system": a.system, "n": a.n, "reps": a.reps}
    trial = trial_shcadi if shc else trial_conv

    log(f"[{a.system} n={a.n}] building...")
    s, t = timed(lambda: build(cls, a.n))
    res["build_s"] = t
    res["memory_mb"] = (proc.memory_info().rss - base) / 1e6
    res["journal_mb"] = s.journal.size_bytes() / 1e6
    res["records"] = len(s.ht)
    if shc:
        res["buckets"] = s.tree.num_buckets

    rng = random.Random(1)
    present = [make_record(i)[0] for i in rng.sample(range(a.n), min(20000, a.n))]
    absent = [make_record(i, "absent")[0] for i in range(min(20000, a.n))]
    _, t = timed(lambda: [s.lookup(h) for h in present])
    res["lookup_present_us"] = t / len(present) * 1e6
    _, t = timed(lambda: [s.lookup(h) for h in absent])
    res["lookup_absent_us"] = t / len(absent) * 1e6
    if shc:
        sub = present[:5000]
        _, t = timed(lambda: [s.verified_lookup(h) for h in sub])
        res["verified_lookup_us"] = t / len(sub) * 1e6

    log("clean verification...")
    clean = []
    for _ in range(max(1, min(a.reps, 3))):
        found, t = timed(s.detect)
        assert not found, "clean index reported corrupt"
        clean.append(t)
    res["verify_clean_s"] = statistics.median(clean)

    sweeps = {}
    for k in [int(x) for x in a.ks.split(",") if x]:
        log(f"fault sweep k={k}")
        sweeps[f"k={k}"] = summarize([trial(s, k, 1000 + r) for r in range(a.reps)])
    for rate in [float(x) for x in a.rates.split(",") if x]:
        k = max(1, int(rate * a.n))
        log(f"corruption rate {rate:.3%} (k={k})")
        sweeps[f"rate={rate}"] = summarize([trial(s, k, 5000)])
        sweeps[f"rate={rate}"]["k_abs"] = k
    res["sweeps"] = sweeps

    if shc:                                                   # read-path detection + single-record repair
        inj = FaultInjector(s.ht, s.tree, seed=77)
        victims = rng.sample(present, 50)
        lat = []
        for h in victims:
            inj.modify_location(h)
            _, t = timed(lambda: s.verified_lookup(h))
            lat.append(t)
        res["readpath_detect_repair_us"] = statistics.median(lat) * 1e6
        res["readpath_index_intact_after"] = not s.detect()

    log("journal shard loss scenario")
    res["shard_loss"] = shard_loss(s, shc, 10, 31337)

    log("insert micro-benchmark")
    new = [make_record(i, "new") for i in range(min(10000, a.n))]
    _, t = timed(lambda: [s.insert(h, e) for h, e in new])
    res["insert_us"] = t / len(new) * 1e6
    res["peak_rss_mb"] = peak_rss_mb()
    out = json.dumps(res)
    if a.out:
        open(a.out, "w").write(out)
    print(out)


if __name__ == "__main__":
    main()
