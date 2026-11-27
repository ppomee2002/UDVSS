import sys
import pandas as pd
import matplotlib.pyplot as plt

# QueryNode sweep QN=1/2/4/8/16 at S=16 (2026-10-01 runs), two segment layouts:
#   M2QN_*    CHUNKS=4  (64 segments)  -- fan-out caps at 4 QN per shard
#   M2QNC16_* CHUNKS=16 (256 segments) -- fan-out reaches 16 QN per shard
#
#   python milvus_qn_scaling_16.py [csv]            # both layouts overlaid
#   python milvus_qn_scaling_16.py [csv] M2QNC16    # one layout only
CSV = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/dev_path/milvus-motivation-results-20261001-qn16/results/runs_m2.csv"
LAYOUTS = {"M2QN": "M=4", "M2QNC16": "M=16"}
TAGS = [sys.argv[2]] if len(sys.argv) > 2 else ["M2QNC16"]
OUT = "milvus_querynode_scaling_16" + ("" if len(TAGS) > 1 else "_" + TAGS[0].lower())

# ============================================
# 1. Load CSV
# ============================================
df = pd.read_csv(CSV)

df["tag"] = df["label"].str.extract(r"^(M2QN|M2QNC16)_(?:ID|VEC)_S16_Q\d+_r\d$")[0]
df["type"] = df["label"].str.extract(r"^M2QN(?:C16)?_(ID|VEC)_")[0]
qn_runs = df[df["tag"].isin(TAGS)].copy()
qn_runs["QN"] = qn_runs["param_N_querynode_pods"]
qn_runs["traffic_kb_q"] = (
    qn_runs["rx_kb_per_query_total"]
    + qn_runs["tx_kb_per_query_total"]
)

summary = (
    qn_runs.groupby(["tag", "type", "QN"])
    .agg(
        mean_ms=("mean_us", lambda x: x.median() / 1000),
        traffic_kb_q=("traffic_kb_q", "median"),
        n_runs=("label", "count")
    )
    .reset_index()
)

print("\n===== QUERYNODE SWEEP SUMMARY (QN=1..16) =====")
print(summary.round(3).to_string(index=False))

# ============================================
# 2. Plot
# ============================================
TYPES = {"ID": ("ID + distance", "#1f77b4", "o"),
         "VEC": ("With raw vector", "#ff7f0e", "s")}
# M=16 solid/filled, M=4 dashed/hollow when overlaid
STYLE = {"M2QNC16": dict(linestyle="-"),
         "M2QN": dict(linestyle="--" if len(TAGS) > 1 else "-",
                      markerfacecolor="white" if len(TAGS) > 1 else None)}
qns = sorted(summary["QN"].unique())

fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.8))

for tag in TAGS:
    for typ, (name, color, marker) in TYPES.items():
        d = summary[(summary["tag"] == tag) & (summary["type"] == typ)].sort_values("QN")
        label = name if len(TAGS) == 1 else f"{name} ({LAYOUTS[tag]})"
        for ax, col in zip(axes, ["traffic_kb_q", "mean_ms"]):
            ax.plot(d["QN"], d[col], color=color, marker=marker,
                    linewidth=1.8, markersize=6, label=label, **STYLE[tag])

# (a) Network traffic
axes[0].set_xlabel("Number of QueryNodes (QN)")
axes[0].set_ylabel("Network Traffic (KB/query)")
axes[0].set_title("(a) Network Traffic")
axes[0].legend(frameon=False, fontsize=7 if len(TAGS) > 1 else None)

# (b) E2E latency
axes[1].set_xlabel("Number of QueryNodes (QN)")
axes[1].set_ylabel("E2E Latency (ms)")
axes[1].set_ylim(0, None)
axes[1].set_title("(b) E2E Latency")

for ax in axes:
    ax.set_xscale("log", base=2)
    ax.set_xticks(qns)
    ax.set_xticklabels([str(q) for q in qns])
    ax.minorticks_off()
    ax.grid(axis="y", linestyle="--", alpha=0.35)

plt.tight_layout()
plt.savefig(OUT + ".pdf", bbox_inches="tight")
plt.savefig(OUT + ".png", dpi=300, bbox_inches="tight")

print("\nSaved:")
print(f"  {OUT}.pdf")
print(f"  {OUT}.png")
