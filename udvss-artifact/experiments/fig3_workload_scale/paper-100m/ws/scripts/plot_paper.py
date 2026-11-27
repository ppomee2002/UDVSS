#!/usr/bin/env python3
# Paper figure in the workload_scale_paper.pdf style (workload-scale-sq8/scripts/plot_paper.py),
# extended with panel (c): (a) Candidates/query, (b) Query latency, (c) Recall@10 vs stored vectors.
# (a),(c): full PG scan rows. (b): tree rows with every limit lifted, mean of 3 reps (C=1).
# pg_num = 1024 everywhere -> stated in the paper caption, not in the figure.
import pandas as pd
import matplotlib.pyplot as plt

plt.rcParams.update({
    "font.size": 13,
    "axes.titlesize": 14,
    "axes.labelsize": 13,
    "xtick.labelsize": 12,
    "ytick.labelsize": 12,
    "legend.fontsize": 11.5,
})

WS = "/home/dev_path/experiments/paper-100m/ws"
df = pd.read_csv(f"{WS}/results/runs.csv")
df = df[(df["failures"] == 0) & (df["missing"] == 0)].copy()
full = df[df["mode"] == "full"].groupby(["m", "n"], as_index=False)[["candidates", "recall"]].first()
tree = df[df["mode"] == "tree"].groupby(["m", "n"], as_index=False)["mean_us"].mean()
tree["mean_ms"] = tree["mean_us"] / 1000
d = full.merge(tree, on=["m", "n"])
print(d.sort_values(["m", "n"]).to_string(index=False))

LINES = {8: ("M=8", "#1f77b4", "o"), 32: ("M=32", "#ff7f0e", "s"),
         96: ("M=96", "#2ca02c", "^"), 384: ("M=384", "#d62728", "D")}

fig, (ax_a, ax_b, ax_c) = plt.subplots(1, 3, figsize=(9.0, 2.6))


def draw(ax, col, label=True):
    for m, (name, color, marker) in LINES.items():
        x = d[d["m"] == m].sort_values("n")
        ax.plot(x["n"], x[col], color=color, marker=marker,
                linewidth=2.2, markersize=7, label=name if label else None)


draw(ax_a, "candidates")
ax_a.legend(frameon=False, handlelength=1.6, handletextpad=0.4,
            labelspacing=0.25, borderaxespad=0.2)
ax_a.set_title("(a) Candidates/query")
ax_a.set_ylabel("Candidates/query", labelpad=2)

draw(ax_b, "mean_ms", label=False)
ax_b.set_title("(b) Query Latency")
ax_b.set_ylabel("Latency (ms)", labelpad=2)

draw(ax_c, "recall", label=False)
ax_c.set_title("(c) Recall@10")
ax_c.set_ylabel("Recall@10", labelpad=2)

for ax in (ax_a, ax_b, ax_c):
    ax.set_xscale("log")
    if ax is not ax_c:
        ax.set_yscale("log")
    ax.set_xticks([1e6, 1e7, 1e8])
    ax.set_xticklabels(["1M", "10M", "100M"])
    ax.minorticks_off()
    ax.set_xlim(7e5, 1.45e8)
    ax.set_xlabel("Number of stored vectors", labelpad=1)
    ax.grid(axis="y", linestyle="--", alpha=0.35)

ax_a.set_ylim(5e3, 3e9)  # headroom so the legend clears the M=384 line
ax_a.set_yticks([1e4, 1e5, 1e6, 1e7, 1e8])
ax_b.set_ylim(2, 3e4)
ax_c.set_ylim(0, 1.0)

fig.subplots_adjust(left=0.075, right=0.982, bottom=0.21, top=0.89, wspace=0.30)
plt.savefig(f"{WS}/figures/workload_scale_paper.pdf")
plt.savefig(f"{WS}/figures/workload_scale_paper.png", dpi=300)
print("Saved figures/workload_scale_paper.{pdf,png}")
