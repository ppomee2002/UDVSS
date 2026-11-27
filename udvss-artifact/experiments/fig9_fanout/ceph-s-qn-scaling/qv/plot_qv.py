#!/usr/bin/env python3
"""The Milvus S / QN figure (2x2) with Qdrant and Vespa added.

(a)(b) partitions S at one serving node: Milvus shards (QN=1), Qdrant shards
       (1 peer). Vespa has no S knob, so it is absent here.
(c)(d) serving nodes at fixed partitioning: Milvus QueryNodes (S=1, 16
       segments), Qdrant peers (16 shards, RF=1), Vespa content nodes (flat).
`plot_qv.py udvss` also draws UDVSS-pivot+SQ8 (OSD sweep; (a)(b) reference).
Per point: median over repetitions, independently per metric (Milvus rule).
"""
import os
import sys

import matplotlib.pyplot as plt
import pandas as pd

EXP = "/home/dev_path/experiments/ceph-s-qn-scaling"
RES = os.path.join(EXP, "qv/results")
M_S = "/home/dev_path/milvus-motivation-results-20261001-qn16/results/runs_m2.csv"
M_Q = "/home/dev_path/milvus-motivation-results-20261006-qn-s1/results/runs_m2.csv"
U_CSV = os.path.join(EXP, "results_sq8/runs.csv")
# argv: [udvss] [samecpu]. samecpu = every system on 109's full CPU budget:
# Qdrant/Vespa unlimited (results_unlimited), UDVSS 126 CPUs split across its
# OSDs (results_sq8_cpu126; N=1 capped at 84 by SeaStore). Milvus is unchanged.
WITH_U = "udvss" in sys.argv[1:]
SAME = "samecpu" in sys.argv[1:]
SIX = "six" in sys.argv[1:]          # 3x2: adds (e)(f) S x QN
if SAME:
    RES = os.path.join(EXP, "qv/results_unlimited")
    U_CSV = os.path.join(EXP, "results_sq8_cpu126/runs.csv")
# m16: UDVSS at M=16 instead of the PG-sweep optimum M=32
M16 = "m16" in sys.argv[1:]
if M16:
    U_CSV = os.path.join(EXP, "results_sq8_cpu126_m16/runs.csv")
# compact: smaller canvas at the same font sizes (tree_pareto_paper style), one-line
# y labels, and a color-per-system + line-style legend instead of 8 entries
COMPACT = "compact" in sys.argv[1:]
# dense (needs six): same 3x2 order as six ((a)(b) / (c)(d) / (e)(f)), same font
# sizes, but tight gaps so the panels get the space. One-line legend on top.
DENSE = "dense" in sys.argv[1:] and SIX
COMPACT = COMPACT or DENSE
plt.rcParams.update({"font.size": 12, "axes.titlesize": 13, "axes.labelsize": 12,
                     "xtick.labelsize": 11, "ytick.labelsize": 11, "legend.fontsize": 9.5})


def agg(df, key):
    return df.groupby(["type", key]).agg(traffic=("traffic", "median"),
                                         ms=("ms", "median"), n=("ms", "count")
                                         ).reset_index().rename(columns={key: "x"})


def milvus(csv, pattern, src, prefer=None):
    d = pd.read_csv(csv)
    r = d[d.label.str.match(pattern)].copy()
    r["type"] = r.label.str.extract(r"_(ID|VEC)_")[0]
    r["traffic"] = r.rx_kb_per_query_total + r.tx_kb_per_query_total
    r["ms"] = r.mean_us / 1000
    r["x"] = r[src]
    if prefer is not None:   # rerun rows replace the original rows of the same (type, x)
        p = d[d.label.str.match(prefer)].copy()
        if len(p):
            p["type"] = p.label.str.extract(r"_(ID|VEC)_")[0]
            p["traffic"] = p.rx_kb_per_query_total + p.tx_kb_per_query_total
            p["ms"] = p.mean_us / 1000
            p["x"] = p[src]
            keys = set(zip(p.type, p.x))
            r = pd.concat([r[[k not in keys for k in zip(r.type, r.x)]], p])
    return agg(r, "x")


def ours(csv, fam):
    if not os.path.exists(csv):
        return pd.DataFrame(columns=["type", "x", "traffic", "ms", "n"])
    d = pd.read_csv(csv)
    d = d[d.family == fam].copy()
    d["type"] = d["mode"]
    d["traffic"] = d.traffic_kb_per_query
    d["ms"] = d.mean_us / 1000
    return agg(d, "shards" if fam == "S" else "nodes")


m_s = milvus(M_S, r"^M2SR_(ID|VEC)_S(1|4|16)_Q1_r\d$", "param_S_num_shards")
m_q = milvus(M_Q, r"^M2QNS1C16_(ID|VEC)_S1_Q\d+_r\d$", "param_N_querynode_pods",
             prefer=r"^M2QNS1C16R_(ID|VEC)_S1_Q\d+_r\d$")
q_s = ours(os.path.join(RES, "qdrant_runs.csv"), "S")
q_q = ours(os.path.join(RES, "qdrant_runs.csv"), "Q")
v_q = ours(os.path.join(RES, "vespa_runs.csv"), "Q")
u_cs = u_cq = None
if WITH_U:
    u = pd.read_csv(U_CSV)
    u = u[(u.route == "pivot") & (u.failures == 0)].copy()
    u["type"], u["traffic"], u["ms"] = u["mode"], u.traffic_kb_per_query, u.mean_us / 1000
    u_cs = agg(u[u.family == "CS"], "osd")
    u_cq = agg(u[u.family == "CQ"], "osd")

COL = {"Milvus": "#1f77b4", "Qdrant": "#9467bd", "Vespa": "#ff7f0e", "UDVSS": "#2ca02c"}
STY = {"ID": ("-", "o", None), "VEC": ("--", "s", "white")}
TN = {"ID": "ID + distance", "VEC": "with raw vector"}


def line(ax, df, col, sysname, label):
    for t in ("ID", "VEC"):
        d = df[df.type == t].sort_values("x")
        if len(d):
            ls, mk, mfc = STY[t]
            ax.plot(d.x, d[col], color=COL[sysname], ls=ls, marker=mk, lw=1.8, ms=5.5,
                    mfc=mfc or COL[sysname], label=f"{sysname} {TN[t]}" if label else None)


# with UDVSS the 8-entry legend needs a wider canvas or it is clipped
if DENSE:
    fig, axes = plt.subplots(3, 2, figsize=(6.6, 7.4))
    (aa, ab), (ac, ad), (ae, af) = axes
elif SIX:
    fig, axes = plt.subplots(3, 2, figsize=(6.4, 6.8) if COMPACT else
                             (9.0, 9.0) if WITH_U else (7.2, 8.6))
    (aa, ab), (ac, ad), (ae, af) = axes
else:
    fig, axes = plt.subplots(2, 2, figsize=(9.0, 6.2) if WITH_U else (7.2, 5.9))
    (aa, ab), (ac, ad) = axes
for ax, col in ((aa, "traffic"), (ab, "ms")):
    line(ax, m_s, col, "Milvus", ax is aa)
    line(ax, q_s, col, "Qdrant", ax is aa)
    if WITH_U:
        for t in ("ID", "VEC"):
            d = u_cs[u_cs.type == t]
            if len(d):
                ax.plot([1, 4, 16], [d[col].iloc[0]] * 3, color=COL["UDVSS"], ls=STY[t][0], lw=1.4)
for ax, col in ((ac, "traffic"), (ad, "ms")):
    line(ax, m_q, col, "Milvus", False)
    line(ax, q_q, col, "Qdrant", False)
    line(ax, v_q, col, "Vespa", False)
    if WITH_U:
        line(ax, u_cq, col, "UDVSS", False)
if SIX:
    # (e)(f) S x QN = N x N. Milvus: S = QN = N on mm_deep1m_s{N}_c16
    # (M2SXQC16). Qdrant: shards = peers = N (family E). Vespa: flat content
    # nodes = N and UDVSS: OSD = N at pg 1024 / M 32 -- both reused from (c)
    # at N = 1/4/16 (same conditions, verified).
    # reused where conditions match: 1x1 = new (c) S=1/QN=1 (16 segments),
    # 16x16 = M2QNC16 S=16/QN=16 (16 segments/shard); only 4x4 is new
    m_11 = m_q[m_q.x == 1]
    m_44 = milvus(M_Q, r"^M2SXQC16_(ID|VEC)_S4_Q4_r\d$", "param_S_num_shards")
    m_1616 = milvus(M_S, r"^M2QNC16_(ID|VEC)_S16_Q16_r\d$", "param_N_querynode_pods")
    m_e = pd.concat([m_11, m_44, m_1616], ignore_index=True)
    # Qdrant: 1x1 and 4x4 measured (family E, 1 segment/shard), 16x16 = (c) peers=16
    q_e = pd.concat([ours(os.path.join(RES, "qdrant_runs.csv"), "E"), q_q[q_q.x == 16]],
                    ignore_index=True)
    for ax, col in ((ae, "traffic"), (af, "ms")):
        line(ax, m_e, col, "Milvus", False)
        line(ax, q_e, col, "Qdrant", False)
        line(ax, v_q[v_q.x.isin([1, 4, 16])], col, "Vespa", False)
        if WITH_U:
            line(ax, u_cq[u_cq.x.isin([1, 4, 16])], col, "UDVSS", False)
# legend entries for systems absent from (a)
# one legend column per system (ID on top, raw vector below), like Milvus/Qdrant
for sysname in (["Vespa", "UDVSS"] if WITH_U else ["Vespa"]):
    for t in ("ID", "VEC"):
        aa.plot([], [], color=COL[sysname], ls=STY[t][0], marker=STY[t][1],
                mfc=STY[t][2] or COL[sysname], label=f"{sysname} {TN[t]}")

for ax, t, xl, tk in ([(aa, "(a) Network Traffic", "Number of Shards (S)", [1, 4, 16]),
                      (ab, "(b) E2E Latency", "Number of Shards (S)", [1, 4, 16]),
                      (ac, "(c) Network Traffic", "Service Nodes", [1, 2, 4, 8, 16]),
                      (ad, "(d) E2E Latency", "Service Nodes", [1, 2, 4, 8, 16])]
                     + ([(ae, "(e) Network Traffic", "S × Service Nodes", [1, 4, 16]),
                         (af, "(f) E2E Latency", "S × Service Nodes", [1, 4, 16])] if SIX else [])):
    ax.set_title(t)
    ax.set_xscale("log", base=2)
    ax.set_xticks(tk)
    ax.set_xticklabels([f"{v}×{v}" if xl == "S × Service Nodes" else str(v) for v in tk])
    ax.minorticks_off()
    ax.set_xlabel(xl)
    ax.grid(axis="y", ls="--", alpha=0.35)
    ax.set_ylim(0, None)
    if DENSE:
        ax.set_title(t, pad=4)
        ax.set_ylabel("Traffic (KB/query)" if "Traffic" in t else "Latency (ms)", labelpad=2)
        ax.set_xlabel(xl, labelpad=1)
        ax.tick_params(pad=2)
        ax.yaxis.set_major_locator(plt.MaxNLocator(nbins=4, integer=True))
    elif COMPACT:
        ax.set_ylabel("Traffic (KB/query)" if "Traffic" in t else "Latency (ms)")
        ax.set_xlabel(xl, labelpad=1)
        ax.tick_params(pad=2)
        ax.yaxis.set_major_locator(plt.MaxNLocator(nbins=4, integer=True))
    else:
        ax.set_ylabel("Network Traffic\n(KB/query)" if "Traffic" in t else "E2E Latency (ms)")
# note: in (e) Milvus 16x16 (~733 KB/q) flattens everyone else, so
# (e) uses a log y-axis; `logf` also puts (f) on log
if SIX:
    # ylim(0, ...) above is invalid on log: reset to the data range
    ae.set_yscale("log")
    ae.set_ylim(0.5, 1500)
    if "logf" in sys.argv[1:]:
        af.set_yscale("log")
        af.set_ylim(0.5, 30)
if COMPACT:
    from matplotlib.lines import Line2D
    sysn = ["Milvus", "Qdrant", "Vespa"] + (["UDVSS"] if WITH_U else [])
    hs = [Line2D([], [], color=COL[n], lw=2.2) for n in sysn]
    hs += [Line2D([], [], color="0.25", ls=STY[t][0], marker=STY[t][1], mfc=STY[t][2] or "0.25",
                  lw=1.6, ms=5) for t in ("ID", "VEC")]
    fig.legend(hs, sysn + [TN["ID"], TN["VEC"]], loc="upper center",
               ncol=len(hs) // 2,
               frameon=False, bbox_to_anchor=(0.5, 1.005), fontsize=10.5, handlelength=1.8,
               handletextpad=0.4, columnspacing=1.0, labelspacing=0.25)
    if DENSE:
        fig.subplots_adjust(left=0.10, right=0.965, bottom=0.055, top=0.905, wspace=0.28, hspace=0.50)
    else:
      fig.subplots_adjust(left=0.115, right=0.965, bottom=0.065, top=0.885, wspace=0.32, hspace=0.55)
else:
    fig.legend(*aa.get_legend_handles_labels(), loc="upper center", ncol=2 if not WITH_U else 4,
               frameon=False, bbox_to_anchor=(0.5, 1.0))
    fig.subplots_adjust(left=0.11 if WITH_U else 0.14, right=0.97, bottom=0.07 if SIX else 0.10,
                        top=(0.86 if not WITH_U else 0.89) if SIX else (0.80 if not WITH_U else 0.84),
                        wspace=0.42, hspace=0.62)
out = os.path.join(EXP, "figures/milvus_qdrant_vespa_s_qn" + ("_udvss" if WITH_U else "")
                   + ("_samecpu" if SAME else "") + ("_6panel" if SIX else "")
                   + ("_logf" if "logf" in sys.argv[1:] else "") + ("_m16" if M16 else "")
                   + ("_dense" if DENSE else "_compact" if COMPACT else ""))
fig.savefig(out + ".pdf")
fig.savefig(out + ".png", dpi=300)
print("saved", out)
for name, df in (("Milvus S", m_s), ("Qdrant S", q_s), ("Milvus QN", m_q), ("Qdrant QN", q_q),
                 ("Vespa QN", v_q)):
    print(f"\n{name}\n", df.round(3).to_string(index=False))
