#!/usr/bin/env python3
"""Paper split/merge figure, split_merge_combined layout, LSH-aligned data
only (results/splitmerge/summary_old_vs_new.csv, new_* columns, exact
values from raw.txt / raw_fullscan.txt).
(a) Split Recall  (b) Split Candidates
(c) Merge Recall  (d) Merge Candidates   -- Fullscan on merge panels only."""
from pathlib import Path
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import pandas as pd

E = Path('/home/dev_path/experiments/lsh-aligned-1m')
df = pd.read_csv(E / 'results/splitmerge/summary_old_vs_new.csv')
SPLIT, MERGE = [128, 256, 512, 1024], [1024, 512, 256, 128]
SERIES = [  # (arm key, M, label, color, marker, linestyle)
    ('tree', 16, 'M=16', '#1f77b4', 'o', '-'),
    ('tree', 32, 'M=32', '#ff7f0e', 's', '-'),
    ('tree', 64, 'M=64', '#2ca02c', '^', '-'),
    ('fullscan', 16, 'M=16 + Fullscan', '#d62728', 'D', '--'),
]
kfmt = FuncFormatter(lambda v, _: f'{v / 1e3:.0f}K')

# Size and type match the other paper figures (ceph-s-qn-scaling
# qv/plot_qv.py: milvus_qdrant_vespa_s_qn, milvus_udvss_sq8_s_qn).
plt.rcParams.update({'font.size': 12, 'axes.titlesize': 13, 'axes.labelsize': 12,
                     'xtick.labelsize': 11, 'ytick.labelsize': 11, 'legend.fontsize': 10.5})
fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.9))
panels = [('split', 'recall', '(a) Split: Recall'), ('split', 'cand', '(b) Split: Candidates'),
          ('merge', 'recall', '(c) Merge: Recall'), ('merge', 'cand', '(d) Merge: Candidates')]
for ax, (path, metric, title) in zip(axes.flat, panels):
    order = SPLIT if path == 'split' else MERGE
    xs = range(len(order))
    for arm, m, label, color, marker, ls in SERIES:
        if arm == 'fullscan' and path == 'split':
            continue
        key = 'fullscan' if arm == 'fullscan' else path
        r = df[(df.arm == key) & (df.M == m)].set_index('pg_num').reindex(order)
        y = r[f'new_{metric}'] * (100 if metric == 'recall' else 1)
        ax.plot(xs, y, color=color, marker=marker, linestyle=ls, linewidth=1.8,
                markersize=5.5, label=label)
    ax.set_xticks(list(xs), [str(n) for n in order])
    ax.set_xlabel('The number of PGs')
    ax.set_ylabel('Recall (%)' if metric == 'recall' else 'Candidates')
    if metric == 'cand':
        ax.yaxis.set_major_formatter(kfmt)
    ax.set_title(title)
    ax.grid(axis='y', linestyle='--', alpha=.35)
h, l = axes[1][0].get_legend_handles_labels()
fig.legend(h, l, loc='upper center', ncol=4, frameon=False, bbox_to_anchor=(0.5, 1.0),
           handlelength=2.4, columnspacing=1.2)
fig.subplots_adjust(left=0.11, right=0.98, bottom=0.10, top=0.89, wspace=0.36, hspace=0.58)
out = E / 'figures'
for ext, kw in (('pdf', {}), ('png', {'dpi': 300})):
    fig.savefig(out / f'split_merge_combined_lsh_aligned.{ext}', **kw)
print('saved', out / 'split_merge_combined_lsh_aligned.{pdf,png}')
