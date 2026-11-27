#!/usr/bin/env python3
"""split/merge figure (Recall / Candidates vs pg_num) for the LSH-aligned
re-measure, in the paper's plot style (plot_selective). Series: M=16/32/64
tree search + M=16 full-PG scan (merge path). Writes
figures/split_merge_lsh_aligned.{pdf,png} (new only) and
figures/split_merge_old_vs_new.{pdf,png} (old dashed, new solid)."""
import sys
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
sys.path.insert(0, '/home/dev_path/lsh-selective-results-20260827')
import plot_selective as P  # noqa: E402

E = Path('/home/dev_path/experiments/lsh-aligned-1m')
df = pd.read_csv(E / 'results/splitmerge/summary_old_vs_new.csv')
PG = [128, 256, 512, 1024]
SERIES = [('tree', 16, 'M=16', '#1f77b4', 'o'), ('tree', 32, 'M=32', '#ff7f0e', 's'),
          ('tree', 64, 'M=64', '#9467bd', '^'), ('fullscan', 16, 'M=16 Fullscan', '#333333', 'D')]


def rows(path, kind, m):
    arm = 'fullscan' if kind == 'fullscan' else path
    r = df[(df.arm == arm) & (df.M == m)].set_index('pg_num')
    return r.reindex(PG if path == 'split' else PG[::-1])


def draw(name, with_old):
    P.style()
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.4), layout='constrained')
    for col, path in enumerate(('split', 'merge')):
        order = PG if path == 'split' else PG[::-1]
        xs = list(range(len(order)))
        for row, (metric, scale, ylab) in enumerate(
                (('recall', 100, 'Recall@10 (%)'), ('cand', 1e-3, 'Candidates/query (k)'))):
            ax = axes[row][col]
            for kind, m, label, color, marker in SERIES:
                if kind == 'fullscan' and path == 'split':
                    continue
                r = rows(path, kind, m)
                P.line(ax, xs, r[f'new_{metric}'] * scale, (label, color, marker))
                if with_old:
                    ax.plot(xs, r[f'old_{metric}'] * scale, color=color, marker=marker,
                            linestyle='--', linewidth=1.2, markersize=4, alpha=.45,
                            markerfacecolor='none')
            ax.set_xticks(xs, [str(n) for n in order])
            P.grid(ax, 'PG count (split →)' if path == 'split' else 'PG count (merge →)', ylab)
            if row == 0:
                ax.set_title(f"({'a' if path == 'split' else 'b'}) "
                             f"{'Split 128→1024' if path == 'split' else 'Merge 1024→128'}")
    h, l = axes[0][1].get_legend_handles_labels()
    if with_old:
        h.append(plt.Line2D([], [], color='#777777', linestyle='--', alpha=.6))
        l.append('previous (hashed locators)')
    fig.legend(h, l, loc='outside lower center', ncol=5 if with_old else 4, fontsize=8.5,
               frameon=False)
    P.save(fig, E / 'figures', name, '')


draw('split_merge_lsh_aligned', False)
draw('split_merge_old_vs_new', True)
