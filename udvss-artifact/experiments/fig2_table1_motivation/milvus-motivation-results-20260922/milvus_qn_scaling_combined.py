import pandas as pd
import matplotlib.pyplot as plt

# (a)(b): S sweep    -- 2026-10-01 rerun (22_s_sweep.sh), QN=1, CHUNKS=4, rows M2SR_*_Q1
#                      (replaces the 09-22 M2SN_* points; S=1/4 ID were CV-unstable there)
# (c)(d): QN sweep   -- milvus_qn_scaling_16.py,    2026-10-01, S=16, CHUNKS=16, rows M2QNC16_*
# Compact 2 x 2 for a paper: small canvas, large fonts so text stays legible when placed.

plt.rcParams.update({
    "font.size": 12,
    "axes.titlesize": 13,
    "axes.labelsize": 12,
    "xtick.labelsize": 11,
    "ytick.labelsize": 11,
    "legend.fontsize": 10.5,
})


CSV_1001 = "/home/dev_path/milvus-motivation-results-20261001-qn16/results/runs_m2.csv"


def load(csv, pattern, key, src):
    df = pd.read_csv(csv)
    runs = df[df["label"].str.match(pattern)].copy()
    runs["type"] = runs["label"].str.extract(r"^[A-Z0-9]+_(ID|VEC)_")[0]
    runs[key] = runs[src]
    runs["traffic_kb_q"] = runs["rx_kb_per_query_total"] + runs["tx_kb_per_query_total"]
    return (
        runs.groupby(["type", key])
        .agg(
            mean_ms=("mean_us", lambda x: x.median() / 1000),
            traffic_kb_q=("traffic_kb_q", "median"),
            n_runs=("label", "count")
        )
        .reset_index()
    )


# ============================================
# 1. Load CSV
# ============================================
s_sum = load(CSV_1001, r"^M2SR_(ID|VEC)_S(1|4|16)_Q1_r\d$", "S", "param_S_num_shards")
q_sum = load(CSV_1001, r"^M2QNC16_(ID|VEC)_S16_Q\d+_r\d$", "QN", "param_N_querynode_pods")

print("\n===== (a)(b) S SWEEP (10-01 rerun), QN=1, CHUNKS=4 =====")
print(s_sum.round(3).to_string(index=False))
print("\n===== (c)(d) QN SWEEP, S=16, CHUNKS=16 =====")
print(q_sum.round(3).to_string(index=False))

# ============================================
# 2. Plot
# ============================================
TYPES = {"ID": ("ID + distance", "#1f77b4", "o"),
         "VEC": ("With raw vector", "#ff7f0e", "s")}

fig, axes = plt.subplots(2, 2, figsize=(6.1, 4.6))
(ax_a, ax_b), (ax_c, ax_d) = axes


def draw(ax, summary, key, col, types, label=True):
    for typ in types:
        name, color, marker = TYPES[typ]
        d = summary[summary["type"] == typ].sort_values(key)
        ax.plot(d[key], d[col], color=color, marker=marker,
                linewidth=1.8, markersize=5.5, label=name if label else None)


def legend(ax):
    ax.legend(frameon=False, handlelength=1.6, handletextpad=0.4,
              labelspacing=0.25, borderaxespad=0.2)


# (a) Network traffic vs S
draw(ax_a, s_sum, "S", "traffic_kb_q", ["ID", "VEC"])
legend(ax_a)
ax_a.set_ylim(top=180)  # headroom so the legend clears the S=16 point
ax_a.set_title("(a) Network Traffic")

# (b) E2E latency vs S (ID and raw vector)
draw(ax_b, s_sum, "S", "mean_ms", ["ID", "VEC"], label=False)
ax_b.set_ylim(0, None)
ax_b.set_title("(b) E2E Latency")

# (c) Network traffic vs QN
draw(ax_c, q_sum, "QN", "traffic_kb_q", ["ID", "VEC"])
legend(ax_c)
ax_c.set_title("(c) Network Traffic")

# (d) E2E latency vs QN (ID and raw vector, as in milvus_qn_scaling_16.py)
draw(ax_d, q_sum, "QN", "mean_ms", ["ID", "VEC"], label=False)
ax_d.set_ylim(0, None)
ax_d.set_title("(d) E2E Latency")

for ax, summary, key, xlabel in [
        (ax_a, s_sum, "S", "Number of Shards (S)"),
        (ax_b, s_sum, "S", "Number of Shards (S)"),
        (ax_c, q_sum, "QN", "Number of QueryNodes (QN)"),
        (ax_d, q_sum, "QN", "Number of QueryNodes (QN)")]:
    ticks = sorted(summary[key].unique())
    ax.set_xscale("log", base=2)
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(t) for t in ticks])
    ax.minorticks_off()
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Network Traffic\n(KB/query)" if ax in (ax_a, ax_c) else "E2E Latency (ms)")
    ax.grid(axis="y", linestyle="--", alpha=0.35)

# ============================================
# 3. Save
# ============================================
# explicit margins: (d)'s x label is wider than its panel and tight_layout lets it run off the canvas
fig.subplots_adjust(left=0.15, right=0.93, bottom=0.12, top=0.94, wspace=0.42, hspace=0.62)

plt.savefig("milvus_s_qn_scaling.pdf")  # no bbox_inches="tight": it cropped (d)'s x label
plt.savefig("milvus_s_qn_scaling.png", dpi=300)

print("\nSaved:")
print("  milvus_s_qn_scaling.pdf")
print("  milvus_s_qn_scaling.png")
