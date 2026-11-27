#!/usr/bin/env python3
# Single panel: candidates per query vs N, one line per M (tree search), pg_num fixed 1024.
import csv, collections
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
WS = "/home/dev_path/experiments/workload-scale-sq8"
COL = {8: "#2a78d6", 32: "#eb6834", 96: "#1baf7a", 384: "#eda100"}
INK, MUTED, GRID = "#1f1f1e", "#6b6a63", "#e4e3dc"
d = collections.defaultdict(dict)
for r in csv.DictReader(open(f"{WS}/results/runs.csv")):
    if r["mode"] == "tree" and r["failures"] == "0":
        d[int(r["m"])][int(r["n"])] = float(r["candidates"])
fig, ax = plt.subplots(figsize=(6, 4.4))
for m in sorted(d):
    ns = sorted(d[m]); ys = [d[m][n] for n in ns]
    ax.plot(ns, ys, color=COL[m], lw=2, marker="o", ms=8, mec="white", mew=2, label=f"M={m}")
    ax.annotate(f"M={m}", (ns[-1], ys[-1]), xytext=(7, 0), textcoords="offset points", va="center", fontsize=9, color=INK)
ax.set_xscale("log"); ax.set_yscale("log")
ax.set_xticks([1e6, 1e7, 1e8]); ax.set_xticklabels(["1M", "10M", "100M"])
ax.set_xlim(7e5, 1.6e8)
ax.set_xlabel("Vectors stored (N)", color=INK); ax.set_ylabel("Candidates per query", color=INK)
ax.set_title("Candidates per query vs. stored vectors (pg_num = 1024 fixed)", color=INK, fontsize=11, loc="left")
ax.grid(True, color=GRID, lw=0.8); ax.set_axisbelow(True)
for s in ("top", "right"): ax.spines[s].set_visible(False)
for s in ("left", "bottom"): ax.spines[s].set_color(MUTED)
ax.tick_params(colors=MUTED)
ax.legend(frameon=False, fontsize=9, loc="upper left")
fig.text(0.01, 0.01, "Deep1B prefix, LSH + SQ8, 1 OSD, NQ=1 C=1, 500 queries per point. M = PGs probed per query.",
         fontsize=7.5, color=MUTED)
fig.tight_layout(rect=(0, 0.04, 1, 1))
for ext in ("png", "pdf"):
    fig.savefig(f"{WS}/figures/candidates_vs_n.{ext}", dpi=180)
