import pandas as pd
import matplotlib.pyplot as plt

BASE = "/home/dev_path/milvus-motivation-results-20260922/results"

# ============================================
# 1. Load CSV
# ============================================
df = pd.read_csv(f"{BASE}/runs_m2.csv")

df["type"] = df["label"].str.extract(r"^M2SN_(ID|VEC)_")[0]
df["S"] = df["param_S_num_shards"]
df["QN"] = df["param_N_querynode_pods"]

df["traffic_kb_q"] = (
    df["rx_kb_per_query_total"]
    + df["tx_kb_per_query_total"]
)

# ============================================
# 2. QueryNode sweep
# S=16, M=4, QN=1/2/4
# ============================================
qn_runs = df[
    df["label"].str.match(r"^M2SN_(ID|VEC)_S16_Q(1|2|4)_r\d$")
    & (df["S"] == 16)
    & (df["param_M_insert_chunks"] == 4)
]

summary = (
    qn_runs.groupby(["type", "QN"])
    .agg(
        mean_ms=("mean_us", lambda x: x.median() / 1000),
        traffic_kb_q=("traffic_kb_q", "median"),
        n_runs=("label", "count")
    )
    .reset_index()
)

print("\n===== QUERYNODE SWEEP SUMMARY =====")
print(summary.round(3))

# ============================================
# 3. Plot
# ============================================
id_data = summary[summary["type"] == "ID"].sort_values("QN")
vec_data = summary[summary["type"] == "VEC"].sort_values("QN")

fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.8))

# (a) Network traffic
axes[0].plot(
    id_data["QN"],
    id_data["traffic_kb_q"],
    marker="o",
    linewidth=1.8,
    markersize=6,
    label="ID + distance"
)

axes[0].plot(
    vec_data["QN"],
    vec_data["traffic_kb_q"],
    marker="s",
    linewidth=1.8,
    markersize=6,
    label="+ raw vector"
)

axes[0].set_xticks([1, 2, 4])
axes[0].set_xticklabels(["1", "2", "4"])

axes[0].set_xlabel("Number of QueryNodes (QN)")
axes[0].set_ylabel("Network Traffic (KB/query)")
axes[0].grid(axis="y", linestyle="--", alpha=0.35)
axes[0].legend(frameon=False)
axes[0].set_title("(a) Network Traffic")


# (b) E2E latency
axes[1].plot(
    id_data["QN"],
    id_data["mean_ms"],
    marker="o",
    linewidth=1.8,
    markersize=6
)

axes[1].set_xticks([1, 2, 4])
axes[1].set_xticklabels(["1", "2", "4"])

axes[1].set_xlabel("Number of QueryNodes (QN)")
axes[1].set_ylabel("E2E Latency (ms)")
axes[1].set_ylim(0, 4.8)

axes[1].grid(axis="y", linestyle="--", alpha=0.35)
axes[1].set_title("(b) E2E Latency")

plt.tight_layout()

plt.savefig(
    "milvus_querynode_scaling.pdf",
    bbox_inches="tight"
)

plt.savefig(
    "milvus_querynode_scaling.png",
    dpi=300,
    bbox_inches="tight"
)

print("\nSaved:")
print("  milvus_querynode_scaling.pdf")
print("  milvus_querynode_scaling.png")