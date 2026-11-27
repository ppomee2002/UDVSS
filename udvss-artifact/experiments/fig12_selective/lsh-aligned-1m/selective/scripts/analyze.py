#!/usr/bin/env python3
"""Offline replay of the exact-tree selective run. Never runs experiments.

Reads the per-(query, stage) CSV written by 03_selective.sh and produces
  results/policies.csv  fixed M / tau-relative / ideal rows
  figures/lsh_tree_selective_combined.{pdf,png}
in the same 2x2 layout and style as lsh_tree_selective_combined.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, '/home/dev_path/lsh-selective-results-20260827')
import plot_selective as P  # noqa: E402

THRESHOLDS = [.00002, .00005, .0001, .0002, .0005, .001, .002, .005, .01, .02, .05, .1]
# Plotted subset = the thresholds of the original lsh_tree_selective_combined
# figure; the denser sweep stays in results/policies.csv.
PLOT_THRESHOLDS = [.001, .005, .01, .02, .05, .1]


def summarize(chosen, family, threshold):
    return {'rule_family': family, 'threshold': threshold,
            'query_count': len(chosen),
            'recall_at_10': chosen.recall_at_stage.mean(),
            'avg_M': chosen.cum_m.mean(),
            'avg_candidates': chosen.cum_candidates.mean()}


def stop_at(df, mask):
    """First stage where mask holds, else the last stage."""
    last = df.stage == df.stage.max()
    chosen = df[mask | last].drop_duplicates('query_index')
    if len(chosen) != df.query_index.nunique():
        raise ValueError('incomplete query selection')
    return chosen


def policies(df):
    df = df.sort_values(['query_index', 'stage'])
    rows = []
    for m in sorted(df.cum_m.unique()):
        rows.append(summarize(stop_at(df, df.cum_m >= m), 'fixed', m))
    for t in THRESHOLDS:
        mask = df.tau.notna() & (df.tau_impr_rel < t)
        rows.append(summarize(stop_at(df, mask), 'tau_relative', t))
    ideal = P.ideal_curve(df)
    for _, r in ideal.iterrows():
        rows.append({'rule_family': 'ideal', 'threshold': r.cap,
                     'query_count': df.query_index.nunique(),
                     'recall_at_10': r.recall_at_10, 'avg_M': r.avg_M,
                     'avg_candidates': r.avg_candidates})
    return pd.DataFrame(rows)


def plot(df, pol, out):
    fixed = pol[pol.rule_family == 'fixed'].sort_values('avg_M')
    sel = pol[(pol.rule_family == 'tau_relative') & pol.threshold.isin(PLOT_THRESHOLDS)]
    ideal = pol[pol.rule_family == 'ideal'].sort_values('avg_M')
    fig, axes = P.canvas()
    a, b, c, d = axes.flat
    spec = ('LSH + tree', '#1f77b4', 'o')
    P.line(a, fixed.avg_M, 100 * fixed.recall_at_10, spec)
    P.line(b, fixed.avg_M, fixed.avg_candidates / 1000, spec)
    a.set_title('(a) Recall vs. Probe Budget')
    b.set_title('(b) Candidates vs. Probe Budget')
    P.grid(a, 'PG Probe Budget (M)', 'Recall@10 (%)')
    P.grid(b, 'PG Probe Budget (M)', 'Candidates/query (k)')
    for ax in [a, b]:
        P.mticks(ax, fixed.avg_M)
    for rows, spec, style in [(fixed, P.TREE_RULES['fixed'], '-'),
                              (sel, P.TREE_RULES['tau_relative'], '-'),
                              (ideal, P.IDEAL, '--')]:
        r = rows.sort_values('avg_M')
        P.line(c, r.avg_M, 100 * r.recall_at_10, spec, linestyle=style)
        r = rows.sort_values('avg_candidates')
        P.line(d, r.avg_candidates / 1000, 100 * r.recall_at_10, spec, linestyle=style)
    # Past the full-budget point the ideal cannot rise further.
    last = ideal.iloc[-1]
    for ax, x0, x1 in [(c, last.avg_M, fixed.avg_M.max()),
                       (d, last.avg_candidates / 1000, fixed.avg_candidates.max() / 1000)]:
        ax.plot([x0, x1], [100 * last.recall_at_10] * 2, color=P.IDEAL[1],
                linestyle='--', linewidth=1.8)
    c.set_title('(c) Selective PG Probing')
    d.set_title('(d) Selective Candidate Cost')
    P.grid(c, 'Average PG Probes', 'Recall@10 (%)')
    P.grid(d, 'Candidates/query (k)', 'Recall@10 (%)')
    c.set_xticks([8, 32, 64, 96, 128])
    lo = 5 * int(100 * fixed.recall_at_10.min() // 5)
    for ax in [a, c, d]:
        ax.set_ylim(lo, 100)
    P.legend(c, loc='lower right', fontsize=8)
    P.save(fig, out, 'lsh_tree_selective_combined', '')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('csv', type=Path)
    args = ap.parse_args()
    df = pd.read_csv(args.csv)
    pol = policies(df)
    (HERE / 'figures').mkdir(exist_ok=True)
    pol.to_csv(HERE / 'results' / 'policies.csv', index=False)
    with pd.option_context('display.width', 140, 'display.max_rows', 100):
        print(pol.assign(recall_pct=100 * pol.recall_at_10).round(3).to_string(index=False))
    P.style()
    plot(df, pol, HERE / 'figures')


if __name__ == '__main__':
    main()
