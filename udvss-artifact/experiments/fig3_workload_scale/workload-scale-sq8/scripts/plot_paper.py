#!/usr/bin/env python3
# Paper figure in the milvus_s_qn_scaling.pdf style (milvus_qn_scaling_combined.py):
# (a) Candidates/query and (b) Query latency vs stored vectors, one line per M.
# pg_num = 1024 everywhere -> stated in the paper caption, not in the figure.
import pandas as pd
import matplotlib.pyplot as plt

plt.rcParams.update({
    "font.size": 12,
    "axes.titlesize": 13,
    "axes.labelsize": 12,
    "xtick.labelsize": 11,
    "ytick.labelsize": 11,
    "legend.fontsize": 10.5,
})

WS = "/home/dev_path/experiments/workload-scale-sq8"
df = pd.read_csv(f"{WS}/results/runs.csv")
df = df[(df["mode"] == "tree") & (df["failures"] == 0)].copy()
df["mean_ms"] = df["mean_us"] / 1000
print(df[["m", "n", "candidates", "mean_ms", "recall"]].sort_values(["m", "n"]).to_string(index=False))

LINES = {8: ("M=8", "#1f77b4", "o"), 32: ("M=32", "#ff7f0e", "s"),
         96: ("M=96", "#2ca02c", "^"), 384: ("M=384", "#d62728", "D")}

fig, (ax_a, ax_b) = plt.subplots(1, 2, figsize=(6.1, 2.6))


def draw(ax, col, label=True):
    for m, (name, color, marker) in LINES.items():
        d = df[df["m"] == m].sort_values("n")
        ax.plot(d["n"], d[col], color=color, marker=marker,
                linewidth=1.8, markersize=5.5, label=name if label else None)


draw(ax_a, "candidates")
ax_a.legend(frameon=False, handlelength=1.6, handletextpad=0.4,
            labelspacing=0.25, borderaxespad=0.2)
ax_a.set_title("(a) Candidates/query")
ax_a.set_ylabel("Candidates/query")

draw(ax_b, "mean_ms", label=False)
ax_b.set_title("(b) Query Latency")
ax_b.set_ylabel("Latency (ms)")

for ax in (ax_a, ax_b):
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xticks([1e6, 1e7, 1e8])
    ax.set_xticklabels(["1M", "10M", "100M"])
    ax.minorticks_off()
    ax.set_xlim(6e5, 1.7e8)
    ax.set_xlabel("Number of stored vectors")
    ax.grid(axis="y", linestyle="--", alpha=0.35)

ax_a.set_ylim(8e3, 6e8)  # headroom so the legend clears the M=384 line
ax_b.set_ylim(3, 3e4)

fig.subplots_adjust(left=0.13, right=0.98, bottom=0.22, top=0.88, wspace=0.42)
plt.savefig(f"{WS}/figures/workload_scale_paper.pdf")
plt.savefig(f"{WS}/figures/workload_scale_paper.png", dpi=300)
print("Saved figures/workload_scale_paper.{pdf,png}")
