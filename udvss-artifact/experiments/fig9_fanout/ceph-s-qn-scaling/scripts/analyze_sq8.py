#!/usr/bin/env python3
"""Milvus (FP32 HNSW) vs UDVSS (Ceph VectorNode, SQ8; LSH and pivot routing).

(a)(b) vs S: Milvus lines; UDVSS has no S knob (its pg_num is the
       latency-optimal one from the PG sweep), so it is a horizontal reference.
(c)(d) vs QN: UDVSS OSD = QN at that pg_num.
(e)(f) vs S x QN (S = QN = 1, 4, 16): Milvus grows both; UDVSS OSD = QN.
Per point: median of the repetitions, each metric independently (as Milvus).
"""
import os
import sys

import matplotlib.pyplot as plt
import pandas as pd

EXP = "/home/dev_path/experiments/ceph-s-qn-scaling"
RES = os.path.join(EXP, "results_sq8")
M_CSV = "/home/dev_path/milvus-motivation-results-20261001-qn16/results/runs_m2.csv"
# (c)(d) per review note: QN sweep at S=1 (16 segments), so it is not
# the same experiment as (e). Falls back to the old S=16 sweep until copied in.
M_CSV_S1 = "/home/dev_path/milvus-motivation-results-20261006-qn-s1/results/runs_m2.csv"
# main figure: pivot only (LSH archived); `analyze_sq8.py all` adds LSH back
WITH_LSH = len(sys.argv) > 1 and sys.argv[1] == "all"
SXQ = "/home/dev_path/milvus-motivation-results-20260922/earlier-sxq-report/table_sxq.csv"
plt.rcParams.update({"font.size": 11, "axes.titlesize": 12, "axes.labelsize": 11,
                     "xtick.labelsize": 10, "ytick.labelsize": 10, "legend.fontsize": 9})


def milvus(pattern, src, csv=M_CSV):
    d = pd.read_csv(csv)
    r = d[d.label.str.match(pattern)].copy()
    r["type"] = r.label.str.extract(r"^[A-Z0-9]+_(ID|VEC)_")[0]
    r["x"] = r[src]
    r["traffic"] = r.rx_kb_per_query_total + r.tx_kb_per_query_total
    r["ms"] = r.mean_us / 1000
    return r.groupby(["type", "x"]).agg(traffic=("traffic", "median"),
                                        ms=("ms", "median")).reset_index()


m_s = milvus(r"^M2SR_(ID|VEC)_S(1|4|16)_Q1_r\d$", "param_S_num_shards")
m_q16 = milvus(r"^M2QNC16_(ID|VEC)_S16_Q\d+_r\d$", "param_N_querynode_pods")
if os.path.exists(M_CSV_S1):
    m_q = milvus(r"^M2QNS1C16_(ID|VEC)_S1_Q\d+_r\d$", "param_N_querynode_pods", M_CSV_S1)
    # Q=4/8/16 were rerun (M2QNS1C16R) after Milvus's CV gate flagged them;
    # the rerun replaces the original points
    m_r = milvus(r"^M2QNS1C16R_(ID|VEC)_S1_Q\d+_r\d$", "param_N_querynode_pods", M_CSV_S1)
    keys = set(zip(m_r.type, m_r.x))
    m_q = pd.concat([m_q[[k not in keys for k in zip(m_q.type, m_q.x)]], m_r], ignore_index=True)
    Q_LABEL = "S=1"
else:
    m_q = m_q16
    Q_LABEL = "S=16"
# S x QN diagonal. 1x1: the (a) run; 16x16: the (c) run; 4x4: the only
# Milvus S=Q=4 measurement, the earlier S x Q grid (C=1, ID only).
sxq = pd.read_csv(SXQ)
s44 = sxq[(sxq["C"] == 1) & (sxq["S (set)"] == 4) & (sxq["Q (set)"] == 4)].iloc[0]
m_d = pd.concat([
    m_s[m_s.x == 1],
    pd.DataFrame([{"type": "ID", "x": 4, "traffic": s44["RX KB/q"] + s44["TX KB/q"],
                   "ms": s44["mean (ms)"]}]),
    m_q16[m_q16.x == 16],
    # no raw-vector run at 4x4: a NaN breaks the line instead of drawing a
    # straight 1x1 -> 16x16 segment through a point that was never measured
    pd.DataFrame([{"type": "VEC", "x": 4, "traffic": float("nan"), "ms": float("nan")}])],
    ignore_index=True)

u = pd.read_csv(os.path.join(RES, "runs.csv"))
u = u[u.failures == 0]
u["ms"] = u.mean_us / 1000


def udvss(route, fam):
    r = u[(u.route == route) & (u.family == fam)]
    return r.groupby(["mode", "osd", "pg", "m"]).agg(
        traffic=("traffic_kb_per_query", "median"), ms=("ms", "median"),
        p99=("p99_us", lambda s: s.median() / 1000), recall=("recall", "median"),
        n=("label", "count")).reset_index().rename(columns={"mode": "type", "osd": "x"})


ROUTES = [r for r in (("lsh", "pivot") if WITH_LSH else ("pivot",))
          if len(u[(u.route == r) & (u.family == "CQ")])]
U = {r: (udvss(r, "CS"), udvss(r, "CQ")) for r in ROUTES}

# ---------------------------------------------------------------- tables
with open(os.path.join(RES, "SUMMARY_SQ8.md"), "w") as f:
    pgs = u[u.family == "PGS"].groupby(["route", "pg", "m"]).agg(
        recall=("recall", "median"), ms=("ms", "median"),
        cand=("candidates", "median")).reset_index()
    f.write("## PG sweep (OSD=1, ID, 500 queries)\n\n| route | pg_num | M | recall | ms | candidates/q |\n|---|---:|---:|---:|---:|---:|\n")
    for _, r in pgs.iterrows():
        f.write(f"| {r.route} | {r.pg} | {r.m} | {r.recall:.4f} | {r.ms:.2f} | {r.cand:,.0f} |\n")
    for r in [x for x in ("lsh", "pivot") if len(u[(u.route == x) & (u.family == "CQ")])]:
        cs, cq = udvss(r, "CS"), udvss(r, "CQ")
        f.write(f"\n## UDVSS-{r} (pg_num {int(cq.pg.iloc[0])}, M {int(cq.m.iloc[0])})\n\n"
                "| OSD | type | KB/q | ms | p99 ms | recall | reps |\n|---:|---|---:|---:|---:|---:|---:|\n")
        for _, x in cq.sort_values(["x", "type"]).iterrows():
            f.write(f"| {x.x} | {x.type} | {x.traffic:.2f} | {x.ms:.2f} | {x.p99:.2f} | {x.recall:.4f} | {x.n} |\n")
print(open(os.path.join(RES, "SUMMARY_SQ8.md")).read())

# ---------------------------------------------------------------- figure
COL = {"Milvus": "#1f77b4", "lsh": "#d62728", "pivot": "#2ca02c"}
NAME = {"Milvus": "Milvus", "lsh": "UDVSS-LSH", "pivot": "UDVSS-pivot"}
STY = {"ID": ("-", "o", None), "VEC": ("--", "s", "white")}
TN = {"ID": "ID + distance", "VEC": "with raw vector"}


def line(ax, df, col, sysname, label):
    for t in ("ID", "VEC"):
        d = df[df.type == t].sort_values("x")
        if not len(d):
            continue
        ls, mk, mfc = STY[t]
        ax.plot(d.x, d[col], color=COL[sysname], ls=ls, marker=mk, lw=1.7, ms=5,
                mfc=mfc or COL[sysname], label=f"{NAME[sysname]} {TN[t]}" if label else None)


def hline(ax, cs, col, route, xs):
    for t in ("ID", "VEC"):
        d = cs[cs.type == t]
        if len(d):
            ax.plot(xs, [d[col].iloc[0]] * len(xs), color=COL[route],
                    ls=STY[t][0], lw=1.4, alpha=0.9)


fig, axes = plt.subplots(3, 2, figsize=(7.0, 8.4))
(aa, ab), (ac, ad), (ae, af) = axes
for ax, col in ((aa, "traffic"), (ab, "ms")):
    line(ax, m_s, col, "Milvus", ax is aa)
    for r in ROUTES:
        hline(ax, U[r][0], col, r, [1, 4, 16])
    # legend entries for the UDVSS reference lines
    if ax is aa:
        for r in ROUTES:
            for t in ("ID", "VEC"):
                ax.plot([], [], color=COL[r], ls=STY[t][0], marker=STY[t][1],
                        mfc=STY[t][2] or COL[r], label=f"{NAME[r]} {TN[t]}")
for ax, col in ((ac, "traffic"), (ad, "ms")):
    line(ax, m_q, col, "Milvus", False)
    for r in ROUTES:
        line(ax, U[r][1], col, r, False)
for ax, col in ((ae, "traffic"), (af, "ms")):
    line(ax, m_d, col, "Milvus", False)
    for r in ROUTES:
        line(ax, U[r][1][U[r][1].x.isin([1, 4, 16])], col, r, False)

titles = ["(a) Traffic vs S", "(b) Latency vs S", f"(c) Traffic vs QN (Milvus {Q_LABEL})",
          f"(d) Latency vs QN (Milvus {Q_LABEL})", "(e) Traffic vs S × QN", "(f) Latency vs S × QN"]
xl = ["Shards S (UDVSS: optimal pg_num)"] * 2 + ["QueryNodes / OSDs"] * 2 + ["S × QN  (S = QN; UDVSS OSD = QN)"] * 2
ticks = [[1, 4, 16]] * 2 + [[1, 2, 4, 8, 16]] * 2 + [[1, 4, 16]] * 2
for ax, t, x, tk in zip(axes.flat, titles, xl, ticks):
    ax.set_title(t)
    ax.set_xscale("log", base=2)
    ax.set_xticks(tk)
    ax.set_xticklabels([f"{v}×{v}" if "×" in t else str(v) for v in tk])
    ax.minorticks_off()
    ax.set_xlabel(x)
    ax.grid(axis="y", ls="--", alpha=0.35)
for ax in (aa, ac, ae):
    ax.set_ylabel("Traffic (KB/query)")
    ax.set_ylim(0, None)
for ax in (ab, ad, af):
    ax.set_ylabel("E2E latency (ms)")
    ax.set_yscale("log")
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi * 1.6)
fig.legend(*aa.get_legend_handles_labels(), loc="upper center", ncol=3,
           frameon=False, bbox_to_anchor=(0.5, 1.0))
fig.subplots_adjust(left=0.12, right=0.97, bottom=0.06, top=0.885, wspace=0.38, hspace=0.62)
os.makedirs(os.path.join(EXP, "figures"), exist_ok=True)
out = os.path.join(EXP, "figures/milvus_udvss_sq8_s_qn" + ("_with_lsh" if WITH_LSH else ""))
fig.savefig(out + ".pdf")
fig.savefig(out + ".png", dpi=300)
print("saved", out)
