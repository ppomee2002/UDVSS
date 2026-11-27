#!/usr/bin/env python3
"""Experiment 2: CRUSH-style hash placement vs UDVSS (pivot), Deep 100M, PG 16384.
Recall@10 vs measured candidates per query, with selected probe counts M
annotated on each curve. One pass of the 1000 queries per M.
usage: plot_exp2.py <exp2_udvss.csv> <exp2_crush.csv> <outdir> [--udvss-full runs.csv]"""
import argparse
import sys
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
sys.path.insert(0, '/home/dev_path/lsh-selective-results-20260827')
import plot_selective as P  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('udvss', type=Path)
ap.add_argument('crush', type=Path)
ap.add_argument('outdir', type=Path)
ap.add_argument('--udvss-full', type=Path,
                help='runs.csv holding the UDVSS M=16384 (b256_r*) rows')
a = ap.parse_args()

def load(p, name):
    d = pd.read_csv(p)
    d = d[d.status == 'OK'][['m', 'recall', 'cand']].copy()
    d['arm'] = name
    return d

u = load(a.udvss, 'UDVSS')
if a.udvss_full:
    f = pd.read_csv(a.udvss_full)
    f = f[(f.status == 'OK') & (f.scale == '100m') & (f.m == 16384) & f.tag.str.startswith('b256')]
    if len(f):
        u = pd.concat([u, pd.DataFrame({'m': [16384], 'recall': [f.recall.mean()],
                                        'cand': [f.cand.mean()], 'arm': ['UDVSS']})])
c = load(a.crush, 'CRUSH (hash)')
d = pd.concat([u, c]).sort_values(['arm', 'm'])
a.outdir.mkdir(parents=True, exist_ok=True)
d.to_csv(a.outdir / 'exp2_summary.csv', index=False)
piv = d.pivot(index='m', columns='arm', values=['recall', 'cand'])
print(piv.to_string())

STY = {'UDVSS': ('UDVSS', '#1f77b4', 'o'), 'CRUSH (hash)': ('CRUSH', '#ff7f0e', 's')}
P.style()
# Match the larger type used by the standalone OSD scaling panel.
plt.rcParams.update({'font.size': 16, 'axes.labelsize': 18,
                     'xtick.labelsize': 16, 'ytick.labelsize': 16,
                     'legend.fontsize': 13.5})
fig, ax = plt.subplots(figsize=(4.6, 3.8), layout='constrained')
for arm, g in d.groupby('arm'):
    P.line(ax, g.cand, g.recall, STY[arm])
    for r in g[g.m.isin([1, 16, 256, 4096])].itertuples():
        offset = (5, -20) if arm == 'UDVSS' else (5, 9)
        if arm == 'UDVSS' and r.m == 256:
            offset = (0, -19)
        if r.m == 4096:
            offset = (-5, -36) if arm == 'UDVSS' else (-5, 9)
        ax.annotate(f'M={r.m}', (r.cand, r.recall),
                    xytext=offset, textcoords='offset points',
                    ha='right' if r.m == 4096 else ('center' if r.m == 256 else 'left'),
                    fontsize=12, color=STY[arm][1])
# Both methods scan the full 100M vectors at M=16384.
full = d[d.m == 16384]
if len(full):
    ax.annotate('M=16384', (full.cand.mean(), full.recall.mean()),
                xytext=(-18, -56), textcoords='offset points', ha='right',
                fontsize=12, color='#444444',
                arrowprops={'arrowstyle': '-', 'color': '#777777', 'lw': 0.7})
ax.set_xscale('log')
ax.set_xticks([1e4, 1e5, 1e6, 1e7, 1e8],
              labels=['10K', '100K', '1M', '10M', '100M'])
ax.minorticks_off()
ax.tick_params(axis='x', labelsize=14)
ax.set_ylim(-0.05, 1.05)
P.grid(ax, 'candidates / query', 'recall@10')
h, l = ax.get_legend_handles_labels()
fig.legend(h, l, loc='outside lower center', ncol=len(h), fontsize=13.5, frameon=False)
P.save(fig, a.outdir, 'crush_vs_udvss_100m', '')
