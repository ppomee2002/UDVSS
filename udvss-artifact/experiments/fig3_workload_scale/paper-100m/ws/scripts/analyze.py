#!/usr/bin/env python3
"""Workload scaling (paper-100m/ws): SUMMARY.md + figures/workload_scale_latency.{pdf,png}.

full rows -> recall / candidates (exact full-PG scan; latency not used: client-side
             expansion, one request per bucket object).
tree rows -> latency (OSD tree, every limit lifted; M requests/query; 3 reps, C=1).
1m rows are the single-pool redo (22_redo_1m.sh); the superseded 1m rows are not used.
"""
import csv, collections, statistics as st, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

WS = "/home/dev_path/experiments/paper-100m/ws"
rows = [r for r in csv.DictReader(open(f"{WS}/results/runs.csv"))
        if r["failures"] == "0" and r["missing"] == "0"]
N = {"1m": 1_000_000, "10m": 10_000_000, "100m": 100_000_000}
MS = [8, 32, 96, 384]
g = collections.defaultdict(list)
for r in rows:
    g[(r["scale"], r["mode"], int(r["m"]))].append(r)

def ms(r, k): return float(r[k]) / 1000.0

summ = {}
for s in N:
    for m in MS:
        f = g[(s, "full", m)][0]
        t = sorted(g[(s, "tree", m)], key=lambda r: int(r["rep"]))
        agg = {k: [ms(r, k) for r in t] for k in ("mean_us", "p50_us", "p95_us", "p99_us")}
        summ[(s, m)] = dict(
            recall=float(f["recall"]), cand=float(f["candidates"]),
            t_recall=float(t[0]["recall"]), t_cand=st.mean(float(r["candidates"]) for r in t),
            reps=len(t), req=float(t[0]["osd_req"]),
            **{k: st.mean(v) for k, v in agg.items()},
            **{k + "_sd": (st.stdev(v) if len(v) > 1 else 0.0) for k, v in agg.items()},
            per_rep_mean=agg["mean_us"])

os.makedirs(f"{WS}/figures", exist_ok=True)
with open(f"{WS}/results/SUMMARY.md", "w") as o:
    o.write("# Workload scaling (pg_num 1024 fixed, LSH + SQ8, --lsh-aligned-locators)\n\n")
    o.write("recall / candidates: full-PG scan. Latency: OSD tree with every limit lifted "
            "(C=1, warmup 50 + 500 queries, mean over 3 reps; ± = stdev across reps).\n\n")
    o.write("| N | M | recall@10 | cand/q (full) | ×1M | tree cand / full | mean ms | p50 | p95 | p99 | ×1M (mean) |\n")
    o.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for m in MS:
        for s in N:
            x = summ[(s, m)]; b = summ[("1m", m)]
            o.write(f"| {s.upper()} | {m} | {x['recall']:.3f} | {x['cand']:,.0f} | {x['cand']/b['cand']:.1f} | "
                    f"{100*x['t_cand']/x['cand']:.1f}% | {x['mean_us']:.1f} ± {x['mean_us_sd']:.1f} | "
                    f"{x['p50_us']:.1f} | {x['p95_us']:.1f} | {x['p99_us']:.1f} | {x['mean_us']/b['mean_us']:.1f} |\n")
    o.write("\n## Per-rep tree mean latency (ms)\n\n| N | M | rep1 | rep2 | rep3 |\n|---|---:|---:|---:|---:|\n")
    for s in N:
        for m in MS:
            o.write(f"| {s.upper()} | {m} | " + " | ".join(f"{v:.1f}" for v in summ[(s, m)]["per_rep_mean"]) + " |\n")

# Figure: (a) candidates/query (full PG), (b) tree latency p50 with p95-p99 range.
COL = {8: "#2a78d6", 32: "#eb6834", 96: "#1baf7a", 384: "#eda100"}
MK = {8: "o", 32: "s", 96: "^", 384: "D"}
INK, MUTED, GRID = "#1f1f1e", "#6b6a63", "#e6e5df"
plt.rcParams.update({"font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
                     "xtick.color": MUTED, "ytick.color": MUTED})
fig, (a, b, c) = plt.subplots(1, 3, figsize=(13.2, 3.5))
xs = [N[s] for s in N]
for m in MS:
    a.plot(xs, [summ[(s, m)]["cand"] for s in N], color=COL[m], marker=MK[m], lw=2, ms=7,
           markeredgecolor="white", markeredgewidth=1.2)
    a.annotate(f"M={m}", (xs[-1], summ[("100m", m)]["cand"]), xytext=(6, 0),
               textcoords="offset points", va="center", fontsize=9, color=INK)
    p50 = [summ[(s, m)]["p50_us"] for s in N]
    lo = [summ[(s, m)]["p95_us"] for s in N]; hi = [summ[(s, m)]["p99_us"] for s in N]
    b.vlines(xs, p50, hi, color=COL[m], lw=1.2, alpha=0.7)
    b.plot(xs, p50, color=COL[m], marker=MK[m], lw=2, ms=7, markeredgecolor="white",
           markeredgewidth=1.2, label=f"M={m}")
    b.annotate(f"M={m}", (xs[-1], p50[-1]), xytext=(6, 0), textcoords="offset points",
               va="center", fontsize=9, color=INK)
    rc = [summ[(s, m)]["recall"] for s in N]
    c.plot(xs, rc, color=COL[m], marker=MK[m], lw=2, ms=7, markeredgecolor="white", markeredgewidth=1.2)
    c.annotate(f"M={m}", (xs[-1], rc[-1]), xytext=(6, 0), textcoords="offset points",
               va="center", fontsize=9, color=INK)
for ax, t, yl in ((a, "(a) Candidates per query (full PG scan)", "Candidates / query"),
                  (b, "(b) Query latency (p50; whisker to p99)", "Latency (ms)"),
                  (c, "(c) Recall@10 (full PG scan)", "Recall@10")):
    ax.set_xscale("log")
    if ax is not c: ax.set_yscale("log")
    ax.set_xticks(xs); ax.set_xticklabels(["1M", "10M", "100M"])
    ax.set_xlim(0.6e6, 3.0e8)
    ax.set_xlabel("Number of stored vectors"); ax.set_ylabel(yl)
    ax.set_title(t, loc="left", fontsize=10, color=INK)
    ax.grid(True, which="major", color=GRID, lw=0.8); ax.set_axisbelow(True)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
b.legend(frameon=False, fontsize=9, loc="upper left")
c.set_ylim(0, 1.0)
fig.tight_layout()
for ext in ("pdf", "png"):
    fig.savefig(f"{WS}/figures/workload_scale_latency.{ext}", dpi=200)
print(open(f"{WS}/results/SUMMARY.md").read())
