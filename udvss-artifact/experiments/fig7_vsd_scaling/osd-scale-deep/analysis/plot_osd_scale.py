#!/usr/bin/env python3
"""QPS vs OSD count (exp 1), one panel per Deep1B scale.
Mean of reps r1-r3 at the chosen C; dashed gray = linear scaling from N=1.
usage: plot_osd_scale.py <runs.csv> <outdir> [scales] [--no-title] [--no-linear]"""
import argparse
import sys
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
sys.path.insert(0, '/home/dev_path/lsh-selective-results-20260827')
import plot_selective as P  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('csv', type=Path)
parser.add_argument('outdir', type=Path)
parser.add_argument('scales', nargs='?', default='1m,10m,100m')
parser.add_argument('--no-title', action='store_true')
parser.add_argument('--no-linear', action='store_true')
args = parser.parse_args()
CSV, OUT = args.csv, args.outdir
SCALES = args.scales.split(',')
NS = [1, 4, 8, 12]
SERIES = [(16, 'M=16', '#1f77b4', 'o'), (32, 'M=32', '#ff7f0e', 's')]
TITLE = {'1m': 'Deep 1M', '10m': 'Deep 10M', '100m': 'Deep 100M'}

df = pd.read_csv(CSV)
# 2026-10-22: "M = max" (every PG, full scan), 12 OSDs only, C=1 B=256
mm = (df[(df.status == 'OK') & df.tag.str.match(r'b256_r\d')]
        .groupby(['scale', 'osd', 'm']).qps.mean().reset_index())
df = df[(df.status == 'OK') & df.tag.str.match(r'r\d')]
s = (df.groupby(['scale', 'osd', 'm'])
       .agg(qps=('qps', 'mean'), qps_sd=('qps', 'std'), recall=('recall', 'mean'),
            osd_cores=('osd_cores', 'mean'), client_cores=('client_cores', 'mean'),
            c=('c', 'first'), n=('qps', 'size'))
       .reset_index())
base = s[s.osd == 1].set_index(['scale', 'm']).qps
s['speedup'] = [r.qps / base.get((r.scale, r.m), float('nan')) for r in s.itertuples()]
s['efficiency'] = s.speedup / s.osd
s['osd_util'] = s.osd_cores / (s.osd * 6)
OUT.mkdir(parents=True, exist_ok=True)
s.to_csv(OUT / 'osd_scale_summary.csv', index=False)
print(s.round(3).to_string(index=False))

P.style()
# Larger type for standalone panels displayed beside the split/merge figure.
plt.rcParams.update({'font.size': 16, 'axes.labelsize': 18,
                     'xtick.labelsize': 16, 'ytick.labelsize': 16,
                     'legend.fontsize': 13.5})
present = [x for x in SCALES if x in set(s.scale)]
figsize = (4.2, 3.4) if len(present) == 1 else (3.3 * len(present), 3.0)
fig, axes = plt.subplots(1, len(present), figsize=figsize,
                         layout='constrained', squeeze=False)
for ax, sc, tag in zip(axes[0], present, 'abc'):
    for m, label, color, marker in SERIES:
        r = s[(s.scale == sc) & (s.m == m)].set_index('osd').reindex(NS)
        P.line(ax, NS, r.qps, (label, color, marker))
        b = r.qps.get(1)
        if pd.notna(b) and not args.no_linear:
            ax.plot(NS, [b * n for n in NS], ls='--', lw=1, color='#9a9a9a', zorder=1)
        last = r.dropna(subset=['qps'])
        if len(last):
            n_last = last.index[-1]
            ax.annotate(f"{last.qps.iloc[-1] / b:.1f}×", (n_last, last.qps.iloc[-1]),
                        textcoords='offset points', xytext=(-4, 6), ha='right',
                        fontsize=12, color='#444444')
    for r in mm[mm.scale == sc].itertuples():
        # Illustrative proportional scaling anchored at the measured endpoint.
        # Intermediate values are assumptions, not additional measurements.
        ax.plot([NS[0], r.osd], [r.qps * NS[0] / r.osd, r.qps],
                ls='--', lw=1.8, color='#2ca02c', zorder=5,
                label='Full PG')
        ax.plot([r.osd], [r.qps], ls='none', marker='o', ms=6,
                color='#2ca02c', markeredgecolor='white', markeredgewidth=1,
                zorder=6)
        ax.annotate(f"{r.qps:.1f}", (r.osd, r.qps),
                    textcoords='offset points', xytext=(-6, 8), ha='right',
                    fontsize=12, color='#444444')
    ax.set_xticks(NS)
    if sc == '100m':
        ax.set_yticks([0, 500, 1000, 1500, 2000])
    max_qps = s[s.scale == sc].qps.max()
    # Keep the low Full PG reference visible above the bottom axis spine.
    ax.set_ylim(-0.03 * max_qps if len(mm[mm.scale == sc]) else 0,
                1.16 * max_qps)
    if not args.no_title:
        rec = s[s.scale == sc].groupby('m').recall.mean()
        ax.set_title(f"({tag}) {TITLE.get(sc, sc)}  "
                     f"(R@10 {rec.get(16, float('nan')):.2f} / {rec.get(32, float('nan')):.2f})",
                     fontsize=9)
    P.grid(ax, 'VSD count', 'QPS')
h, l = [], []
for ax in axes[0]:
    for hh, ll in zip(*ax.get_legend_handles_labels()):
        if ll not in l:
            h.append(hh); l.append(ll)
if not args.no_linear:
    h.append(plt.Line2D([], [], ls='--', lw=1, color='#9a9a9a'))
    l.append('linear (from 1 OSD)')
if len(present) == 1:
    # Preserve the plot box from the original two-row bottom-legend layout.
    original_legend = fig.legend(h, l, loc='outside lower center',
                                 ncol=2, fontsize=13.5, frameon=False)
    fig.canvas.draw()
    box = axes[0, 0].get_window_extent()
    axes[0, 0].set_box_aspect(box.height / box.width)
    original_legend.remove()
    axes[0, 0].legend(h, l, loc='lower center', bbox_to_anchor=(0.5, 1.02),
                      ncol=len(h) if args.no_linear else 2,
                      fontsize=13.5, frameon=False, borderaxespad=0,
                      columnspacing=0.7, handlelength=1.3, handletextpad=0.5)
else:
    fig.legend(h, l, loc='outside upper center', ncol=len(h),
               fontsize=13.5, frameon=False,
               columnspacing=0.7, handlelength=1.3, handletextpad=0.4)
P.save(fig, OUT, 'osd_scale_qps_' + '_'.join(present), '')
