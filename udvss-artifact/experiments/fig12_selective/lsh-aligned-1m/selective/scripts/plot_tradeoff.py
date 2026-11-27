#!/usr/bin/env python3
"""Recall-cost trade-off figure for selective probing (offline replay only).

(a) Recall@10 vs average PG probes, (b) Recall@10 vs candidates/query.
Fixed M is the baseline; tau-relative selective with patience 1/2/3 are
the policies; the ground-truth ideal is a grey dashed upper bound.
"""
import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

import analyze as A

P = A.P
THRESHOLDS = [.1, .05, .02, .01, .005, .001]
LABELED = {.01: 'τ=.01', .001: 'τ=.001'}
POLICIES = [  # (patience, label, color, marker)
    (1, 'Selective', '#1f77b4', 's'),
    (2, 'Selective + patience 2', '#ff7f0e', '^'),
    (3, 'Selective + patience 3', '#9467bd', 'D'),
]


def patience_stop(df, threshold, patience):
    """Stop once tau improved by less than threshold on `patience`
    consecutive stages; queries that never qualify run every stage."""
    ok = df.tau.notna() & (df.tau_impr_rel < threshold)
    run_id = (ok != ok.groupby(df.query_index).shift()).cumsum()
    streak = ok.groupby([df.query_index, run_id]).cumsum() * ok
    return A.stop_at(df, streak >= patience)


def sweep(df):
    rows = []
    for patience, *_ in POLICIES:
        for t in THRESHOLDS:
            r = A.summarize(patience_stop(df, t, patience), 'selective', t)
            rows.append({**r, 'patience': patience})
    return pd.DataFrame(rows)


def panel(ax, x, fixed, sel, ideal, scale):
    P.line(ax, fixed[x] / scale, 100 * fixed.recall_at_10, ('Fixed M', '#333333', 'o'))
    for patience, label, color, marker in POLICIES:
        rows = sel[sel.patience == patience].sort_values(x)
        P.line(ax, rows[x] / scale, 100 * rows.recall_at_10, (label, color, marker))
        if patience == 1:
            for _, r in rows[rows.threshold.isin(LABELED)].iterrows():
                ax.annotate(LABELED[r.threshold], (r[x] / scale, 100 * r.recall_at_10),
                            xytext=(9, -3), textcoords='offset points',
                            ha='left', va='center', fontsize=8, color=color,
                            bbox=dict(boxstyle='round,pad=.15', fc='white',
                                      ec='none', alpha=.85), zorder=5)
    ideal = ideal.sort_values(x)
    xs = list(ideal[x] / scale) + [fixed[x].max() / scale]
    ys = list(100 * ideal.recall_at_10) + [100 * ideal.recall_at_10.iloc[-1]]
    ax.plot(xs, ys, color='#9a9a9a', linestyle='--', linewidth=1.4,
            label='Ideal (GT, upper bound)', zorder=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('csv', type=Path)
    ap.add_argument('--name', default='selective_tradeoff')
    args = ap.parse_args()
    df = pd.read_csv(args.csv).sort_values(['query_index', 'stage'])
    pol = A.policies(df)
    fixed = pol[pol.rule_family == 'fixed'].sort_values('avg_M')
    ideal = pol[pol.rule_family == 'ideal']
    sel = sweep(df)
    out = A.HERE / 'results'
    sel.to_csv(out / f'{args.name}_policies.csv', index=False)
    print(sel.assign(recall_pct=100 * sel.recall_at_10).round(3).to_string(index=False))

    P.style()
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.8, 3.0), layout='constrained')
    panel(a, 'avg_M', fixed, sel, ideal, 1)
    panel(b, 'avg_candidates', fixed, sel, ideal, 1000)
    a.set_title('(a) Recall vs. PG Probes')
    b.set_title('(b) Recall vs. Candidate Cost')
    P.grid(a, 'Average PG Probes', 'Recall@10 (%)')
    P.grid(b, 'Candidates/query (k)', 'Recall@10 (%)')
    a.set_xticks([8, 32, 64, 96, 128])
    # Zoom on the region where the policies differ; the smallest fixed M
    # may fall below the axis and its segment is then clipped.
    lo = 100 * sel.recall_at_10.min()
    hi = 100 * fixed.recall_at_10.max()
    pad = max(1.0, (hi - lo) * .08)
    for ax in (a, b):
        ax.set_ylim(lo - pad, min(100, hi + pad))
    P.legend(b, loc='lower right', fontsize=7.5)
    (A.HERE / 'figures').mkdir(exist_ok=True)
    P.save(fig, A.HERE / 'figures', args.name, '')


if __name__ == '__main__':
    main()
