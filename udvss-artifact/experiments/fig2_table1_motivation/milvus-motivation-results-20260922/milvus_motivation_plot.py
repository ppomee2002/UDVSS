import pandas as pd
import matplotlib.pyplot as plt

plt.rcParams.update({
    "font.size": 15,
    "axes.titlesize": 16,
    "axes.labelsize": 17,
    "xtick.labelsize": 15,
    "ytick.labelsize": 15,
    "legend.fontsize": 12,
    "font.weight": "medium"
})

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
# 2. S sweep
# QN=1, M=4, S=1/4/16
# ============================================
s_runs = df[
    df["label"].str.match(r"^M2SN_(ID|VEC)_S(1|4|16)_Q1_r\d$")
    & (df["QN"] == 1)
    & (df["param_M_insert_chunks"] == 4)
]

summary = (
    s_runs.groupby(["type", "S"])
    .agg(
        mean_ms=("mean_us", lambda x: x.median() / 1000),
        traffic_kb_q=("traffic_kb_q", "median"),
        n_runs=("label", "count")
    )
    .reset_index()
)

print("\n===== S SWEEP SUMMARY =====")
print(summary.round(3))

# ============================================
# 3. Plot
# ============================================
id_data = summary[summary["type"] == "ID"].sort_values("S")
vec_data = summary[summary["type"] == "VEC"].sort_values("S")

fig = plt.figure(figsize=(7.2, 3.3))

# ------------------------------------------------
# 실제 그래프 박스 위치를 직접 지정
#
# [왼쪽, 아래, 폭, 높이]
#
# 마지막 0.58이 "그래프만 세로로 짧게" 만드는 부분
# ------------------------------------------------
ax0 = fig.add_axes([0.14, 0.23, 0.36, 0.58])
ax1 = fig.add_axes([0.61, 0.23, 0.36, 0.58])

# ============================================
# (a) Network Traffic
# ============================================
ax0.plot(
    id_data["S"],
    id_data["traffic_kb_q"],
    marker="o",
    linewidth=1.8,
    markersize=6,
    label="ID + distance"
)

ax0.plot(
    vec_data["S"],
    vec_data["traffic_kb_q"],
    marker="s",
    linewidth=1.8,
    markersize=6,
    label="With raw vector"
)

ax0.set_xscale("log", base=2)
ax0.set_xticks([1, 4, 16])
ax0.set_xticklabels(["1", "4", "16"])
ax0.minorticks_off()

ax0.set_xlabel("Number of Shards (S)")

# 중요:
# 여기서는 set_ylabel() 사용하지 않음

ax0.grid(
    axis="y",
    linestyle="--",
    alpha=0.35
)

ax0.legend(
    frameon=False,
    loc="upper left",
    bbox_to_anchor=(0.02, 0.98),
    handlelength=1.8,
    handletextpad=0.5,
    labelspacing=0.3,
    borderaxespad=0.0,
    prop={
        "size": 12
    }
)

ax0.set_title("(a) Network Traffic")

# ============================================
# (b) E2E Latency
# ============================================
ax1.plot(
    id_data["S"],
    id_data["mean_ms"],
    marker="o",
    linewidth=1.8,
    markersize=6
)

ax1.set_xscale("log", base=2)
ax1.set_xticks([1, 4, 16])
ax1.set_xticklabels(["1", "4", "16"])
ax1.minorticks_off()

ax1.set_xlabel("Number of Shards (S)")

# 이것도 set_ylabel() 사용하지 않음
ax1.set_ylim(0, 4.5)

ax1.grid(
    axis="y",
    linestyle="--",
    alpha=0.35
)

ax1.set_title("(b) E2E Latency")

# ============================================
# 4. Y labels
# 그래프 박스가 아니라 전체 figure 기준으로 배치
# -> 그래프를 납작하게 해도 글씨 안 잘림
# ============================================

fig.text(
    0.035,
    0.52,
    "Network Traffic (KB/query)",
    rotation=90,
    va="center",
    ha="center",
    fontsize=17
)

fig.text(
    0.545,
    0.52,
    "E2E Latency (ms)",
    rotation=90,
    va="center",
    ha="center",
    fontsize=17
)

# ============================================
# 5. Save
# ============================================

# 여기서는 bbox_inches="tight"도 일부러 사용하지 않음.
# 우리가 지정한 figure 영역 그대로 저장해서
# 글자가 다시 잘리는 일을 막음.
plt.savefig(
    "milvus_shard_scaling.pdf"
)

plt.savefig(
    "milvus_shard_scaling.png",
    dpi=300
)

print("\nSaved:")
print("  milvus_shard_scaling.pdf")
print("  milvus_shard_scaling.png")