#!/usr/bin/env python3
# tree_pareto_paper_m32 (tree-ablation-1m/scripts/plot_pareto.py) for the
# hierarchical-LSH index: same panel, same styling. Full PG curve = measured
# full-PG scans over M (PCHIP between them); no offline fill.
# usage: plot_pareto_hier.py M
import re, sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.interpolate import PchipInterpolator

plt.rcParams.update({"font.size": 12, "axes.titlesize": 13, "axes.labelsize": 12,
                     "xtick.labelsize": 11, "ytick.labelsize": 11, "legend.fontsize": 10.5})
T = "/home/dev_path/experiments/hier-lsh-1m"
M = int(sys.argv[1])
TARGET = 0.90


def parse(path):
    rows = []
    for line in open(path):
        tag = line.split()[0]
        kv = dict(re.findall(r" ([a-z0-9_]+)=([-0-9.e]+)", line))
        rows.append({"tag": tag, "recall": float(kv["recall_at_top_k"]),
                     "cand": float(kv["avg_total_matching_records_per_query"]),
                     "onodes": float(kv.get("tree_opened_onodes_per_query", "nan"))})
    return pd.DataFrame(rows)


df = parse(f"{T}/results/pareto_raw_m{M}.txt")
df[["budget", "onode"]] = df["tag"].str.extract(r"pareto_b(\d+)_o(\d+)").astype(int)
# MIN_ONODE: leave the low-ONode tail out of the figure (kept in the table).
import os
df_all = df
df = df[(df.onode == 0) | (df.onode >= int(os.environ.get("MIN_ONODE", 0)))].copy()
fcurve = parse(f"{T}/results/m_scan_raw.txt")
fcurve["M"] = fcurve.tag.str.extract(r"_m(\d+)$").astype(int)[0]
fcurve = fcurve.sort_values("M")
full = fcurve.set_index("M").loc[M]

ok = df[df.recall >= TARGET].sort_values("cand")
table = df_all.sort_values(["budget", "onode"]).copy()
table["onode"] = table["onode"].map(lambda o: "unlimited" if o == 0 else o)
table["cand_vs_full_%"] = (table.cand / full.cand - 1) * 100
table["recall_vs_full_pp"] = (table.recall - full.recall) * 100
with open(f"{T}/results/PARETO_m{M}.md", "w") as f:
    f.write(f"## Hierarchical LSH: Recall-Candidates Pareto (M={M}, patience 4, 1000 q)\n\n"
            f"Full PG reference: recall {full.recall:.4f}, candidates/q {full.cand:.0f}\n\n"
            "| budget | onode | Recall@10 | Candidates/q | ONode/q | Cand vs Full | Recall vs Full (%p) |\n|---|---|---|---|---|---|---|\n")
    for _, r in table.iterrows():
        f.write(f"| {r.budget} | {r.onode} | {r.recall:.4f} | {r.cand:.0f} | {r.onodes:.1f} | "
                f"{r['cand_vs_full_%']:+.1f}% | {r.recall_vs_full_pp:+.2f} |\n")
    if len(ok):
        p = ok.iloc[0]
        f.write(f"\nBest point with recall >= {TARGET}: budget {p.budget}, onode {p.onode or 'unlimited'} -> "
                f"recall {p.recall:.4f}, candidates/q {p.cand:.0f} ({(p.cand / full.cand - 1) * 100:+.1f}% vs Full PG)\n")
print(open(f"{T}/results/PARETO_m{M}.md").read())

STYLE = {4: ("Tree budget 4", "#1f77b4", "o", (5, -11), "left"),
         8: ("Tree budget 8", "#ff7f0e", "s", (-5, 5), "right")}
xlo = df.cand.min() / 1e3 * 0.9
xhi = max(df.cand.max(), full.cand) / 1e3 * 1.06
fig, ax = plt.subplots(figsize=(5.0, 3.4))
for b, (name, color, marker, off, ha) in STYLE.items():
    d = df[df.budget == b].copy()
    d["key"] = (d.cand / 1e3).round(0).astype(str) + "/" + d.recall.round(3).astype(str)
    groups = []
    for _, g in d.groupby("key"):
        vals = sorted(g.onode, key=lambda o: o if o else 10**9)
        if len(vals) == 1:
            txt = "∞" if vals[0] == 0 else str(vals[0])
        else:
            txt = f"≥{vals[0]}" if vals[-1] == 0 else f"{vals[0]}–{vals[-1]}"
        groups.append((g.cand.mean() / 1e3, g.recall.mean(), txt))
    groups.sort()
    xs, ys, _ = zip(*groups)
    ax.plot(xs, ys, color=color, marker=marker, linewidth=1.8, markersize=5.5, label=name, clip_on=False)
    for x_, y_, t in groups:
        ax.annotate(t, (x_, y_), textcoords="offset points", xytext=off, ha=ha,
                    fontsize=8, color=color, annotation_clip=False)
    if b == 4:
        # Budget 4 stops improving at its last ONode budget (recall and
        # candidates stay put), so its ceiling runs flat out to the end of
        # the budget-8 curve, as in tree_pareto_paper_m32.
        ax.plot([xs[-1], df[df.budget == 8].cand.max() / 1e3], [ys[-1], ys[-1]],
                color=color, linewidth=1.8)
mm = np.linspace(fcurve.M.min(), fcurve.M.max(), 400)
ax.plot(PchipInterpolator(fcurve.M, fcurve.cand)(mm) / 1e3, PchipInterpolator(fcurve.M, fcurve.recall)(mm),
        color="#d62728", linestyle="--", linewidth=1.6, zorder=3)
ax.plot(fcurve.cand / 1e3, fcurve.recall, color="#d62728", marker="*", markersize=10, linestyle="none", zorder=4)
ax.plot([], [], color="#d62728", marker="*", markersize=10, linestyle="--", linewidth=1.6, label="Full PG scan")
for _, r in fcurve.iterrows():
    if xlo <= r.cand / 1e3 <= xhi:
        ax.annotate(f"M={r.M}", (r.cand / 1e3, r.recall), textcoords="offset points",
                    xytext=(6, -12), ha="left", fontsize=8, color="#d62728")
ax.set_xlim(xlo, xhi)
ylo = min(df.recall.min(), fcurve[(fcurve.cand / 1e3 >= xlo)].recall.min()) - 0.02
ax.set_ylim(ylo, full.recall + 0.02)
ax.set_xlabel("Candidates/query (×10³)")
ax.set_ylabel("Recall@10")
ax.grid(axis="y", linestyle="--", alpha=0.35)
ax.legend(frameon=False, handlelength=1.6, handletextpad=0.4, labelspacing=0.25, borderaxespad=0.2,
          loc="lower right", fontsize=9, title="label = ONode budget", title_fontsize=8.5)
fig.subplots_adjust(left=0.16, right=0.97, bottom=0.16, top=0.96)
plt.savefig(f"{T}/figures/tree_pareto_paper_hier_m{M}.pdf")
plt.savefig(f"{T}/figures/tree_pareto_paper_hier_m{M}.png", dpi=300)
print(f"Saved figures/tree_pareto_paper_hier_m{M}.{{pdf,png}}")
