#!/usr/bin/env python3
# Workload scale (N = 1M/10M/100M, PG fixed 1024, LSH+SQ8): candidates, latency
# and recall against N, one line per M; solid = full PG scan, dashed = tree.
# Writes figures/workload_scale_sq8.{png,pdf} and results/SUMMARY.md.
import csv, os, collections
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

WS = "/home/dev_path/experiments/workload-scale-sq8"
rows = [r for r in csv.DictReader(open(f"{WS}/results/runs.csv")) if r["failures"] == "0"]
COL = {8: "#2a78d6", 32: "#eb6834", 96: "#1baf7a", 384: "#eda100"}  # categorical slots 1-4
INK, MUTED, GRID = "#1f1f1e", "#6b6a63", "#e4e3dc"

data = collections.defaultdict(dict)  # (mode, m) -> n -> row
for r in rows:
    data[(r["mode"], int(r["m"]))][int(r["n"])] = r

panels = [("candidates", "Candidates scanned / query", 1, True),
          ("mean_us", "Mean latency / query (ms)", 1e-3, True),
          ("recall", "Recall@10", 1, False)]
fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))
for ax, (key, title, scale, logy) in zip(axes, panels):
    for (mode, m), byn in sorted(data.items()):
        if mode != "tree":  # full scan is identical wherever both ran (see SUMMARY)
            continue
        ns = sorted(byn)
        ys = [float(byn[n][key]) * scale for n in ns]
        ax.plot(ns, ys, color=COL.get(m, MUTED), lw=2, marker="o", ms=8, mec="white", mew=2,
                label=f"M={m} of 1024 PGs probed")
        if key != "recall":
            ax.annotate(f"M={m}", (ns[-1], ys[-1]), xytext=(6, 0), textcoords="offset points",
                        va="center", fontsize=9, color=INK)
    if key == "candidates":  # slope-1 guide: what "proportional to N" looks like
        full = [byn for (mode, m), byn in data.items() if mode == "tree"]
        if full:
            b = min(full, key=lambda d: min(float(v["candidates"]) for v in d.values()))
            n0 = min(b); y0 = float(b[n0]["candidates"])
            xs = [1e6, 1e7, 1e8]
            ax.plot(xs, [y0 * x / n0 for x in xs], color=MUTED, lw=1, ls=":", label="∝ N")
    ax.set_xscale("log"); ax.set_yscale("log" if logy else "linear")
    ax.set_xticks([1e6, 1e7, 1e8]); ax.set_xticklabels(["1M", "10M", "100M"])
    ax.set_xlabel("Vectors stored (N)", color=INK); ax.set_title(title, color=INK, fontsize=11, loc="left")
    ax.grid(True, color=GRID, lw=0.8); ax.set_axisbelow(True)
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    for s in ("left", "bottom"): ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=MUTED)
axes[2].set_ylim(0, 1.02)
h, l = axes[0].get_legend_handles_labels()
fig.legend(h, l, loc="lower center", bbox_to_anchor=(0.5, 0.045), ncol=5, frameon=False, fontsize=9)
pairs = [(float(byn[n]["candidates"]), float(data[("tree", m)][n]["candidates"]),
          byn[n]["recall"] == data[("tree", m)][n]["recall"])
         for (mode, m), byn in data.items() if mode == "full" for n in byn if n in data.get(("tree", m), {})]
maxrel = max((abs(f - t) / t for f, t, _ in pairs), default=0)
fig.text(0.01, 0.01, f"Tree search (budget 64, patience 4). Against a full PG scan at the same (N, M), {len(pairs)} points: "
         f"candidates differ by at most {maxrel:.1e} (relative), recall identical in {sum(r for *_, r in pairs)}/{len(pairs)}; "
         "every probed PG is scanned (almost) completely.", fontsize=8, color=MUTED)
fig.suptitle("Deep1B prefix, LSH + SQ8, pg_num fixed at 1024, 1 OSD, NQ=1 C=1", color=INK, fontsize=11, x=0.01, ha="left")
fig.tight_layout(rect=(0, 0.13, 1, 0.95))
os.makedirs(f"{WS}/figures", exist_ok=True)
for ext in ("png", "pdf"):
    fig.savefig(f"{WS}/figures/workload_scale_sq8.{ext}", dpi=160)

with open(f"{WS}/results/SUMMARY.md", "w") as f:
    f.write("| mode | M | N | recall | candidates/q | ×1M | mean ms | p99 ms | ×1M |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for (mode, m), byn in sorted(data.items()):
        ns = sorted(byn); c0 = float(byn[ns[0]]["candidates"]); l0 = float(byn[ns[0]]["mean_us"])
        for n in ns:
            r = byn[n]
            f.write(f"| {mode} | {m} | {n//1_000_000}M | {float(r['recall']):.3f} | {float(r['candidates']):,.0f} | "
                    f"{float(r['candidates'])/c0:.1f} | {float(r['mean_us'])/1e3:.1f} | {float(r['p99_us'])/1e3:.1f} | "
                    f"{float(r['mean_us'])/l0:.1f} |\n")
print(open(f"{WS}/results/SUMMARY.md").read())
