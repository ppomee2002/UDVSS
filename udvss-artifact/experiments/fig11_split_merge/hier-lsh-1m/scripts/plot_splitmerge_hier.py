#!/usr/bin/env python3
"""split_merge_lsh_aligned (lsh-aligned-1m/scripts/33_plot_splitmerge.py) for the
hierarchical-LSH run: same 2x2 layout and series (M=16/32/64 tree, M=16 full-PG
scan on the merge path), read straight from 31_splitmerge.sh's raw files.
usage: plot_splitmerge_hier.py <tag>   (results/splitmerge_<tag>/)"""
import re, sys
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
sys.path.insert(0, '/home/dev_path/lsh-selective-results-20260827')
import plot_selective as P  # noqa: E402

E = Path('/home/dev_path/experiments/hier-lsh-1m')
TAG = sys.argv[1]
D = E / f'results/splitmerge_{TAG}'
PG = [128, 256, 512, 1024]
SERIES = [('tree', 16, 'M=16', '#1f77b4', 'o'), ('tree', 32, 'M=32', '#ff7f0e', 's'),
          ('tree', 64, 'M=64', '#9467bd', '^'), ('fullscan', 16, 'M=16 Fullscan', '#333333', 'D')]


def parse(path, kind):
    rows = []
    if not path.exists():
        return rows
    for line in open(path):
        tag = line.split()[0].lstrip('f')
        kv = dict(re.findall(r'(\w+)=([-0-9.e]+)', line))
        rows.append({'kind': kind, 'path': tag.split('_')[0], 'pg_num': int(kv['pg']),
                     'M': int(kv['m']), 'recall': float(kv['recall_at_top_k']),
                     'cand': float(kv['avg_total_matching_records_per_query'])})
    return rows


df = pd.DataFrame(parse(D / 'raw.txt', 'tree') + parse(D / 'raw_fullscan.txt', 'fullscan'))
df.to_csv(D / 'summary.csv', index=False)
print(df.pivot_table(index=['kind', 'path', 'M'], columns='pg_num', values=['recall', 'cand']).round(3))

P.style()
fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.4), layout='constrained')
for row, path in enumerate(('split', 'merge')):
    order = PG if path == 'split' else PG[::-1]
    xs = list(range(len(order)))
    for col, (metric, scale, ylab) in enumerate(
            (('recall', 100, 'Recall@10 (%)'), ('cand', 1e-3, 'Candidates/query (k)'))):
        ax = axes[row][col]
        for kind, m, label, color, marker in SERIES:
            if kind == 'fullscan' and path == 'split':
                continue
            r = df[(df.kind == kind) & (df.path == path) & (df.M == m)].set_index('pg_num').reindex(order)
            P.line(ax, xs, r[metric] * scale, (label, color, marker))
        ax.set_xticks(xs, [str(n) for n in order])
        P.grid(ax, 'PG count (split →)' if path == 'split' else 'PG count (merge →)', ylab)
        ax.set_title(f"({'abcd'[2 * row + col]}) "
                     f"{'Split 128→1024' if path == 'split' else 'Merge 1024→128'}: "
                     f"{'recall' if metric == 'recall' else 'candidates'}")
h, l = axes[1][0].get_legend_handles_labels()
fig.legend(h, l, loc='outside lower center', ncol=4, fontsize=8.5, frameon=False)
P.save(fig, E / 'figures', f'split_merge_hier_{TAG}', '')
