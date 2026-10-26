"""Plots generated strictly from measured benchmark results (results.json)."""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

C = {"shcadi": "#1b7f5c", "conventional": "#c0392b", "conventional_targeted": "#e67e22"}
L = {"shcadi": "SH-CADI", "conventional": "Conventional (full rebuild)",
     "conventional_targeted": "Conventional + targeted repair"}
SYS = ("shcadi", "conventional", "conventional_targeted")


def _runs(data):
    out = {}
    for r in data["runs"]:
        if "failed" not in r:
            out[(r["system"], r["n"])] = r
    return out


def _sizes(runs):
    return sorted({n for (_, n) in runs})


def _med(sw, key, field="median"):
    return sw[key][field]


def recovery_vs_faults(data, outdir):
    runs = _runs(data)
    sizes = _sizes(runs)
    fig, axes = plt.subplots(1, len(sizes), figsize=(5 * len(sizes), 4), sharey=False)
    axes = [axes] if len(sizes) == 1 else axes
    for ax, n in zip(axes, sizes):
        for s in SYS:
            r = runs.get((s, n))
            if not r:
                continue
            ks = sorted(int(k[2:]) for k in r["sweeps"] if k.startswith("k="))
            med = [r["sweeps"][f"k={k}"]["recovery_phase_s"]["median"] for k in ks]
            lo = [r["sweeps"][f"k={k}"]["recovery_phase_s"]["min"] for k in ks]
            hi = [r["sweeps"][f"k={k}"]["recovery_phase_s"]["max"] for k in ks]
            ax.plot(ks, med, "o-", color=C[s], label=L[s])
            ax.fill_between(ks, lo, hi, color=C[s], alpha=0.2)
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_title(f"{n:,} records"); ax.set_xlabel("corrupted records (k)")
        ax.grid(True, which="both", alpha=0.3)
    axes[0].set_ylabel("recovery phase time (s)\nlocalize + recover + repair + verify")
    axes[0].legend()
    fig.suptitle("Recovery-phase time vs number of faults (median, band = min-max over repetitions)")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "recovery_vs_faults.png"), dpi=130)
    plt.close(fig)


def rate_sweep(data, outdir):
    runs = _runs(data)
    n = 100_000 if ("shcadi", 100_000) in runs else (_sizes(runs) or [None])[-1]
    a, b = runs.get(("shcadi", n)), runs.get(("conventional", n))
    if not a or not b:
        return
    rates = sorted(float(k[5:]) for k in a["sweeps"] if k.startswith("rate="))
    if not rates:
        return
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    x = [r * 100 for r in rates]
    ax1.plot(x, [a["sweeps"][f"rate={r}"]["locality"]["median"] * 100 for r in rates], "o-", color=C["shcadi"], label=L["shcadi"])
    ax1.plot(x, [100.0] * len(x), "o-", color=C["conventional"], label=L["conventional"])
    t = runs.get(("conventional_targeted", n))
    if t:
        ax1.plot(x, [t["sweeps"][f"rate={r}"]["locality"]["median"] * 100 for r in rates], "^--", color=C["conventional_targeted"], label=L["conventional_targeted"])
    ax1.set_xscale("log"); ax1.set_yscale("log")
    ax1.set_xlabel("corruption rate (% of records)"); ax1.set_ylabel("recovery locality (% of records reconstructed)")
    ax1.grid(True, which="both", alpha=0.3); ax1.legend()
    for s, r in (("shcadi", a), ("conventional", b), ("conventional_targeted", runs.get(("conventional_targeted", n)))):
        if r is None:
            continue
        ax2.plot(x, [r["sweeps"][f"rate={q}"]["recovery_phase_s"]["median"] for q in rates], "o-", color=C[s], label=L[s])
    ax2.set_xscale("log"); ax2.set_yscale("log")
    ax2.set_xlabel("corruption rate (% of records)"); ax2.set_ylabel("recovery phase time (s)")
    ax2.grid(True, which="both", alpha=0.3); ax2.legend()
    fig.suptitle(f"Corruption-rate sweep, {n:,} records (single run per point)")
    fig.tight_layout(); fig.savefig(os.path.join(outdir, "rate_sweep.png"), dpi=130); plt.close(fig)


def scalability(data, outdir):
    runs = _runs(data)
    sizes = _sizes(runs)
    panels = [("detect_s", "corruption detection (s)"), ("localize_s", "fault localization (s)"),
              ("recovery_phase_s", "recovery phase (s)")]
    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    for ax, (key, title) in zip(axes, panels):
        for s in SYS:
            ns = [n for n in sizes if (s, n) in runs and "k=100" in runs[(s, n)]["sweeps"]]
            ax.plot(ns, [runs[(s, n)]["sweeps"]["k=100"][key]["median"] for n in ns], "o-", color=C[s], label=L[s])
        ax.set_xscale("log"); ax.set_yscale("log"); ax.set_title(title)
        ax.set_xlabel("records"); ax.grid(True, which="both", alpha=0.3)
    axes[0].legend()
    fig.suptitle("Scalability with 100 corrupted records")
    fig.tight_layout(); fig.savefig(os.path.join(outdir, "scalability.png"), dpi=130); plt.close(fig)


def memory(data, outdir):
    runs = _runs(data)
    sizes = _sizes(runs)
    fig, ax = plt.subplots(figsize=(7, 4))
    w = 0.27
    for i, s in enumerate(SYS):
        xs = [j + (i - 1) * w for j in range(len(sizes))]
        ax.bar(xs, [runs[(s, n)]["memory_mb"] if (s, n) in runs else 0 for n in sizes], w, color=C[s], label=L[s])
    ax.set_xticks(range(len(sizes))); ax.set_xticklabels([f"{n:,}" for n in sizes])
    ax.set_xlabel("records"); ax.set_ylabel("resident memory added by index (MB)")
    ax.set_title("Memory (hash table + journal + integrity structure)"); ax.legend(); ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout(); fig.savefig(os.path.join(outdir, "memory.png"), dpi=130); plt.close(fig)


def normal_ops(data, outdir):
    no = data["normal_ops"]
    names = ["plain", "conventional", "shcadi"]
    labels = ["hash table only", "conventional\n(+journal)", "SH-CADI\n(+journal+Merkle+verify)"]
    fig, ax = plt.subplots(figsize=(6.5, 4))
    ax.bar(labels, [no["configs"][n]["chunks_per_s"] for n in names], color=["#7f8c8d", C["conventional"], C["shcadi"]])
    ax.set_ylabel("dedup upload throughput (chunks/s)")
    ax.set_title(f"Normal operation: {no['chunks']:,} chunks of 4 KB, {no['dup_ratio']:.0%} duplicates")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout(); fig.savefig(os.path.join(outdir, "normal_ops.png"), dpi=130); plt.close(fig)


def localization(data, outdir):
    runs = _runs(data)
    n = 100_000 if ("shcadi", 100_000) in runs else (_sizes(runs) or [None])[-1]
    a = runs.get(("shcadi", n))
    if not a:
        return
    ks = sorted(int(k[2:]) for k in a["sweeps"] if k.startswith("k="))
    fig, ax = plt.subplots(figsize=(6.5, 4))
    ax.plot(ks, [a["sweeps"][f"k={k}"]["nodes_compared"]["median"] for k in ks], "o-", color=C["shcadi"], label="SH-CADI: Merkle nodes compared")
    ax.plot(ks, [a["sweeps"][f"k={k}"]["records_examined"]["median"] for k in ks], "s--", color="#2980b9", label="SH-CADI: records examined")
    ax.axhline(a["records"], color=C["conventional"], label="full scan: all records")
    ax.set_xscale("log"); ax.set_yscale("log"); ax.set_xlabel("corrupted records (k)"); ax.set_ylabel("comparisons / records")
    ax.set_title(f"Localization work, {n:,} records"); ax.grid(True, which="both", alpha=0.3); ax.legend()
    fig.tight_layout(); fig.savefig(os.path.join(outdir, "localization.png"), dpi=130); plt.close(fig)


def make_all(data, outdir):
    os.makedirs(outdir, exist_ok=True)
    for fn in (recovery_vs_faults, rate_sweep, scalability, memory, normal_ops, localization):
        fn(data, outdir)
    return sorted(f for f in os.listdir(outdir) if f.endswith(".png"))
