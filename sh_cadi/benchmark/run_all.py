"""Run the full evaluation.  Every (system, n) executes in its own process; results are written
incrementally to <out>/results.json so a long 1M run never loses earlier results.

    python -m sh_cadi.benchmark.run_all --out benchmark_results
    python -m sh_cadi.benchmark.run_all --sizes 10000,100000 --out benchmark_results
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import time

import psutil

from sh_cadi.benchmark import normal_ops

PLAN = {
    10_000: dict(reps=5, ks="1,10,100", rates="0.01,0.05"),
    100_000: dict(reps=3, ks="1,10,100", rates="0.001,0.01,0.05,0.1,0.25"),
    1_000_000: dict(reps=1, ks="1,10,100", rates=""),
}


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmark_results")
    ap.add_argument("--sizes", default="10000,100000,1000000")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--resume", action="store_true", help="keep finished runs from an existing results.json")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "results.json")
    data = {"environment": {"python": platform.python_version(), "platform": platform.platform(),
                            "cpus": psutil.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 2**30, 2),
                            "started": time.strftime("%Y-%m-%d %H:%M:%S")},
            "normal_ops": None, "runs": []}
    if a.resume and os.path.exists(path):
        data = json.load(open(path))
        data["runs"] = [r for r in data["runs"] if "failed" not in r]
    if data.get("normal_ops") is None:
        print("normal-operation throughput...", flush=True)
        data["normal_ops"] = normal_ops.run()
        save(path, data)
    for n in [int(x) for x in a.sizes.split(",")]:
        for system in ("shcadi", "conventional", "conventional_targeted"):
            if any(r["system"] == system and r["n"] == n for r in data["runs"]):
                print(f"skip {system} n={n} (already done)", flush=True)
                continue
            cfg = PLAN[n]
            cmd = [sys.executable, "-m", "sh_cadi.benchmark.worker", "--system", system, "--n", str(n),
                   "--reps", str(cfg["reps"]), "--ks", cfg["ks"], "--rates", cfg["rates"]]
            print(f"running {system} n={n} ...", flush=True)
            t = time.time()
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
                if p.returncode == 0:
                    rec = json.loads(p.stdout.strip().splitlines()[-1])
                else:
                    rec = {"system": system, "n": n, "failed": f"exit code {p.returncode}",
                           "stderr_tail": p.stderr[-600:]}
            except subprocess.TimeoutExpired:
                rec = {"system": system, "n": n, "failed": f"timeout after {a.timeout}s"}
            rec["wall_s"] = time.time() - t
            data["runs"].append(rec)
            save(path, data)
            print(f"  done in {rec['wall_s']:.0f}s" + (f"  FAILED: {rec['failed']}" if "failed" in rec else ""), flush=True)
    print("all done", flush=True)


if __name__ == "__main__":
    main()
