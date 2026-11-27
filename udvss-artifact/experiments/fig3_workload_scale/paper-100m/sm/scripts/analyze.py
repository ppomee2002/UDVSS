#!/usr/bin/env python3
"""Split/merge full-PG-scan results (paper-100m/sm).

Reads results/<scale>/raw_fullscan.txt and results/<scale>/*.census.err and
writes results/SUMMARY.md and figures/split_merge_fullpg.{pdf,png}
(recall and candidates vs pg_num, split and merge arms, 1M and 100M).
"""
import re, os, glob, collections
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

EXP = "/home/dev_path/experiments/paper-100m/sm"
SCALES = [s for s in ("1m", "100m") if os.path.exists(f"{EXP}/results/{s}/raw_fullscan.txt")]
PGS = [128, 256, 512, 1024]
data = collections.defaultdict(dict)   # (scale, arm, m) -> pg -> kv
census = {}
for s in SCALES:
    for line in open(f"{EXP}/results/{s}/raw_fullscan.txt"):
        tag = line.split()[0]                       # fsplit_pg128 / fmerge_pg1024
        arm = "split" if tag.startswith("fsplit") else "merge"
        pg = int(re.search(r"pg=(\d+)", line).group(1)); m = int(re.search(r" m=(\d+)", line).group(1))
        kv = dict(re.findall(r"(\w+)=(\S+)", line.split("FANOUT_QUERY", 1)[1]))
        data[(s, arm, m)][pg] = kv
    for f in glob.glob(f"{EXP}/results/{s}/*.census.err"):
        t = os.path.basename(f).split(".")[0]
        c = [l for l in open(f) if l.startswith("CENSUS_TOTAL")]
        census[(s, t)] = c[0].strip() if c else "MISSING"

MS = sorted({m for (_, _, m) in data})
with open(f"{EXP}/results/SUMMARY.md", "w") as o:
    o.write("# Split/merge, full PG scan (Deep1B prefix, SQ8, --lsh-aligned-locators)\n\n")
    o.write("150 queries, top-10, C=1, io-depth 0. Split: load at 128, then 256 -> 512 -> 1024. "
            "Merge: load at 1024, then 512 -> 256 -> 128. Each arm has its own fresh pool.\n\n")
    for s in SCALES:
        o.write(f"## {s.upper()}\n\n| M | arm | " + " | ".join(f"pg {p} recall / cand" for p in PGS) + " |\n")
        o.write("|---:|---|" + "---|" * len(PGS) + "\n")
        for m in MS:
            for arm in ("split", "merge"):
                d = data.get((s, arm, m), {})
                cells = [f"{float(d[p]['recall_at_top_k']):.3f} / {float(d[p]['avg_total_matching_records_per_query']):,.0f}"
                         if p in d else "-" for p in PGS]
                o.write(f"| {m} | {arm} | " + " | ".join(cells) + " |\n")
        bad = [(arm, m, p) for (sc, arm, m), d in data.items() if sc == s for p, kv in d.items()
               if kv.get("failures") != "0" or kv.get("missing_objects") != "0"]
        o.write(f"\nfailures / missing_objects != 0: {bad or 'none'}\n\n**Census**\n\n")
        for (sc, t), c in sorted(census.items()):
            if sc == s: o.write(f"- {t}: `{c}`\n")
        o.write("\n")

# Figure: per scale, recall and candidates vs pg_num at two M values; split solid, merge dashed.
COL = {16: "#2a78d6", 64: "#eb6834"}
show = [m for m in (16, 64) if m in MS]
INK, MUTED, GRID = "#1f1f1e", "#6b6a63", "#e6e5df"
plt.rcParams.update({"font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
                     "xtick.color": MUTED, "ytick.color": MUTED})
fig, axes = plt.subplots(len(SCALES), 2, figsize=(9.2, 3.2 * len(SCALES)), squeeze=False)
for i, s in enumerate(SCALES):
    for j, (key, yl) in enumerate((("recall_at_top_k", "Recall@10"),
                                   ("avg_total_matching_records_per_query", "Candidates / query"))):
        ax = axes[i][j]
        for m in show:
            for arm, ls, mk in (("split", "-", "o"), ("merge", "--", "s")):
                d = data.get((s, arm, m), {})
                xs = [p for p in PGS if p in d]
                ax.plot(xs, [float(d[p][key]) for p in xs], ls=ls, marker=mk, color=COL[m], lw=2, ms=6,
                        markeredgecolor="white", markeredgewidth=1.0, label=f"M={m} {arm}")
        ax.set_xscale("log", base=2); ax.set_xticks(PGS); ax.set_xticklabels(PGS)
        ax.set_xlabel("pg_num"); ax.set_ylabel(yl)
        ax.set_title(f"({'ab'[j] if len(SCALES) == 1 else 'abcd'[2*i+j]}) {s.upper()}: {yl}", loc="left", fontsize=10, color=INK)
        ax.grid(True, color=GRID, lw=0.8); ax.set_axisbelow(True)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
axes[0][0].legend(frameon=False, fontsize=8)
fig.tight_layout()
os.makedirs(f"{EXP}/figures", exist_ok=True)
for ext in ("pdf", "png"):
    fig.savefig(f"{EXP}/figures/split_merge_fullpg.{ext}", dpi=200)
print(open(f"{EXP}/results/SUMMARY.md").read())
