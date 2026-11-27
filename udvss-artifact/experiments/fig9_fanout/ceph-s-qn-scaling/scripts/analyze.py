#!/usr/bin/env python3
"""Ceph S/QN sweep: summary table + Milvus overlay of milvus_s_qn_scaling.

Same aggregation rule as milvus_qn_scaling_combined.py: per point, the median
of the repetitions, taken independently for each metric.
Milvus is drawn solid/filled, Ceph dashed/hollow, same colour per reply type.
"""
import os
import sys

import matplotlib.pyplot as plt
import pandas as pd

EXP = "/home/dev_path/experiments/ceph-s-qn-scaling"
MILVUS_CSV = "/home/dev_path/milvus-motivation-results-20261001-qn16/results/runs_m2.csv"
plt.rcParams.update({"font.size": 12, "axes.titlesize": 13, "axes.labelsize": 12,
                     "xtick.labelsize": 11, "ytick.labelsize": 11,
                     "legend.fontsize": 9.5})


def milvus(pattern, key, src):
    df = pd.read_csv(MILVUS_CSV)
    r = df[df["label"].str.match(pattern)].copy()
    r["type"] = r["label"].str.extract(r"^[A-Z0-9]+_(ID|VEC)_")[0]
    r["x"] = r[src]
    r["traffic_kb_q"] = r["rx_kb_per_query_total"] + r["tx_kb_per_query_total"]
    r["mean_ms"] = r["mean_us"] / 1000
    return (r.groupby(["type", "x"]).agg(traffic_kb_q=("traffic_kb_q", "median"),
                                         mean_ms=("mean_ms", "median"),
                                         n=("label", "count")).reset_index())


def ceph(family, xcol):
    df = pd.read_csv(os.path.join(EXP, "results/runs.csv"))
    r = df[(df["family"] == family) & (df["failures"] == 0)].copy()
    r["type"] = r["mode"]
    r["x"] = r[xcol]
    r["mean_ms"] = r["mean_us"] / 1000
    return (r.groupby(["type", "x"]).agg(
        traffic_kb_q=("traffic_kb_per_query", "median"),
        mean_ms=("mean_ms", "median"),
        p99_ms=("p99_us", lambda s: s.median() / 1000),
        recall=("recall", "median"),
        reply_b=("reply_bytes_per_query", "median"),
        n=("label", "count")).reset_index())


m_s = milvus(r"^M2SR_(ID|VEC)_S(1|4|16)_Q1_r\d$", "S", "param_S_num_shards")
m_q = milvus(r"^M2QNC16_(ID|VEC)_S16_Q\d+_r\d$", "QN", "param_N_querynode_pods")
c_s = ceph("CS", "pg")
c_q = ceph("CQ", "osd")

with open(os.path.join(EXP, "results/SUMMARY_TABLE.md"), "w") as f:
    for title, m, c, xl in [("(a)(b) S / PG sweep", m_s, c_s, "S=PG"),
                            ("(c)(d) QN / OSD sweep", m_q, c_q, "QN=OSD")]:
        f.write(f"## {title}\n\n| {xl} | type | Milvus KB/q | Ceph KB/q | Milvus ms | "
                f"Ceph ms | Ceph p99 ms | Ceph recall | Ceph reps |\n|---:|---|---:|---:|---:|---:|---:|---:|---:|\n")
        j = pd.merge(m, c, on=["type", "x"], how="outer", suffixes=("_m", "_c")).sort_values(["x", "type"])
        for _, r in j.iterrows():
            f.write(f"| {int(r.x)} | {r.type} | {r.traffic_kb_q_m:.2f} | {r.traffic_kb_q_c:.2f} | "
                    f"{r.mean_ms_m:.3f} | {r.mean_ms_c:.3f} | {r.p99_ms:.3f} | {r.recall:.4f} | "
                    f"{r.n_c if pd.notna(r.n_c) else 0:.0f} |\n")
        f.write("\n")
print(open(os.path.join(EXP, "results/SUMMARY_TABLE.md")).read())

# colour = system, line style + marker = reply type
SYS = {"Milvus": "#1f77b4", "Ceph": "#d62728"}
STY = {"ID": ("-", "o"), "VEC": ("--", "s")}
NAME = {"ID": "ID + distance", "VEC": "With raw vector"}


def draw(ax, m, c, col, label):
    for sysname, df in (("Milvus", m), ("Ceph", c)):
        for t in ("ID", "VEC"):
            d = df[df.type == t].sort_values("x")
            if not len(d):
                continue
            ls, mk = STY[t]
            ax.plot(d.x, d[col], color=SYS[sysname], ls=ls, marker=mk, lw=1.8,
                    ms=5.5, mfc=SYS[sysname] if t == "ID" else "white",
                    label=f"{sysname} {NAME[t]}" if label else None)


def figure(path, log_latency):
    fig, axes = plt.subplots(2, 2, figsize=(6.6, 5.2))
    (aa, ab), (ac, ad) = axes
    draw(aa, m_s, c_s, "traffic_kb_q", True)
    draw(ab, m_s, c_s, "mean_ms", False)
    draw(ac, m_q, c_q, "traffic_kb_q", False)
    draw(ad, m_q, c_q, "mean_ms", False)
    for ax, t in zip((aa, ab, ac, ad), ("(a) Network Traffic", "(b) E2E Latency",
                                         "(c) Network Traffic", "(d) E2E Latency")):
        ax.set_title(t)
    for ax, xl, ticks in [(aa, "Number of Shards / PGs", [1, 4, 16]),
                          (ab, "Number of Shards / PGs", [1, 4, 16]),
                          (ac, "Number of QNs / OSDs", [1, 2, 4, 8, 16]),
                          (ad, "Number of QNs / OSDs", [1, 2, 4, 8, 16])]:
        ax.set_xscale("log", base=2)
        ax.set_xticks(ticks)
        ax.set_xticklabels([str(t) for t in ticks])
        ax.minorticks_off()
        ax.set_xlabel(xl)
        ax.grid(axis="y", linestyle="--", alpha=0.35)
    for ax in (aa, ac):
        ax.set_ylabel("Network Traffic\n(KB/query)")
        ax.set_ylim(0, None)
    for ax in (ab, ad):
        ax.set_ylabel("E2E Latency (ms)")
        if log_latency:
            ax.set_yscale("log")
        else:
            ax.set_ylim(0, None)
    fig.legend(*aa.get_legend_handles_labels(), loc="upper center", ncol=2,
               frameon=False, bbox_to_anchor=(0.5, 1.0))
    fig.subplots_adjust(left=0.14, right=0.97, bottom=0.10, top=0.86,
                        wspace=0.42, hspace=0.62)
    fig.savefig(path + ".pdf")
    fig.savefig(path + ".png", dpi=300)
    print("saved", path)


os.makedirs(os.path.join(EXP, "figures"), exist_ok=True)
figure(os.path.join(EXP, "figures/milvus_ceph_s_qn_scaling"), log_latency=False)
figure(os.path.join(EXP, "figures/milvus_ceph_s_qn_scaling_loglat"), log_latency=True)
