#!/usr/bin/env python3
"""Paper-style plots from exported historical CSVs only. Never runs experiments."""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).resolve().parent
ROUTING = {
    'baseline': ('Original', '#1f77b4', 'o'),
    'table_only': ('Table only', '#2ca02c', '^'),
    'margin_only': ('Margin only', '#9467bd', 'D'),
    'both': ('Table + margin', '#ff7f0e', 's'),
}
RULES = {
    'fixed': ('Fixed M', '#333333', 'o'),
    'tau_relative': (r'$\tau$ improvement', '#1f77b4', 's'),
    'topk': ('Top-k changes', '#ff7f0e', '^'),
    'tau_over_d1': (r'$\tau/d_1$', '#9467bd', 'D'),
    'oracle': ('Oracle (GT)', '#2ca02c', '*'),
}

# Tree figure keeps only fixed M, the selective rule and the ideal (GT) curve;
# the other stopping rules are described in the text instead.
TREE_RULES = {
    'fixed': ('Fixed', '#333333', 'o'),
    'tau_relative': ('Selective', '#1f77b4', 's'),
}
IDEAL = ('Ideal (GT)', '#2ca02c', '*')
TREE_RAW = HERE / 'raw/home/dev_path/experiments/selective-fanout-sift1m/results/tree_msweep.csv'


def ideal_curve(df):
    """GT oracle at every probe cap: each query stops at the first stage whose
    recall already equals its recall at the cap."""
    rows = []
    for cap in sorted(df.cum_m.unique()):
        prefix = df[df.cum_m <= cap].sort_values(['query_index', 'stage'])
        target = prefix.query_index.map(prefix[prefix.cum_m == cap].set_index('query_index').recall_at_stage)
        chosen = prefix[prefix.recall_at_stage >= target].drop_duplicates('query_index')
        rows.append({'cap': cap, 'avg_M': chosen.cum_m.mean(),
                     'avg_candidates': chosen.cum_candidates.mean(),
                     'recall_at_10': chosen.recall_at_stage.mean()})
    return pd.DataFrame(rows)


def style():
    # Mirrors milvus_qn_scaling_combined.py: large type, compact panels,
    # unboxed legends, 1.8pt lines, 300dpi PNG and vector PDF.
    plt.rcParams.update({'font.size': 12, 'axes.titlesize': 12,
                         'axes.labelsize': 12, 'xtick.labelsize': 10.5,
                         'ytick.labelsize': 10.5, 'legend.fontsize': 9,
                         'pdf.fonttype': 42, 'ps.fonttype': 42})


def grid(ax, xlabel, ylabel):
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(axis='y', linestyle='--', alpha=.35)


def mticks(ax, values):
    ticks = sorted(set(values))
    ax.set_xscale('log', base=2)
    ax.set_xticks(ticks, labels=[str(int(t)) for t in ticks])
    ax.minorticks_off()


def legend(ax, **kwargs):
    return ax.legend(frameon=False, handlelength=1.5, handletextpad=.4,
                     labelspacing=.25, borderaxespad=.2, **kwargs)


def line(ax, x, y, spec, **kwargs):
    label, color, marker = spec
    ax.plot(x, y, label=label, color=color, marker=marker,
            linewidth=1.8, markersize=5, **kwargs)


def save(fig, out, name, note):
    # Experiment conditions are documented in README, not printed on the figure.
    # Constrained layout accommodates labels; fixed canvas, no tight-bbox crop.
    fig.savefig(out / f'{name}.pdf')
    fig.savefig(out / f'{name}.png', dpi=300)
    plt.close(fig)
    print(f'Saved {out / name}.pdf / .png')


def canvas():
    fig, axes = plt.subplots(2, 2, figsize=(6.8, 5.5), layout='constrained')
    fig.get_layout_engine().set(rect=(0, 0, 1, 1), h_pad=.07, w_pad=.05,
                               hspace=.06, wspace=.04)
    return fig, axes


def combined(stages, policies, out):
    fig, axes = canvas()
    a, b, c, d = axes.flat
    for variant, spec in ROUTING.items():
        rows = stages[stages.variant == variant].sort_values('M')
        line(a, rows.M, rows.recall_at_stage_mean, spec)
        line(b, rows.M, rows.cum_candidates_mean / 1000, spec)
    a.set_title('(a) LSH Routing: Recall')
    b.set_title('(b) LSH Routing: Candidates')
    for ax in [a, b]:
        mticks(ax, [8,16,32,64,96])
    grid(a, 'PG Probe Budget (M)', 'Recall@10')
    grid(b, 'PG Probe Budget (M)', 'Candidates/query (k)')
    a.set_ylim(.065, .23)
    legend(a, loc='upper left', ncol=2, fontsize=8)
    for ax, variant, title in [(c, 'baseline', '(c) Original Routing'),
                                (d, 'both', '(d) Table + Margin Routing')]:
        for family, spec in RULES.items():
            rows = policies[(policies.variant == variant) & (policies.rule_family == family)].sort_values('avg_M')
            line(ax, rows.avg_M, rows.recall_at_10, spec,
                 linestyle='None' if family == 'oracle' else '-')
        ax.set_title(title)
        grid(ax, 'Average PG Probes', 'Recall@10')
        ax.set_xticks([8,32,64,96])
        ax.set_ylim(.085, .202)
    legend(c, loc='lower right', fontsize=8)
    save(fig, out, 'lsh_selective_combined',
         'SIFT1M | PG=1024, D=1, 10k queries | Tree OFF\n(c,d): offline stopping-rule replay; oracle requires ground truth')


def parameters(policies, out):
    fig, axes = canvas()
    for row_idx, (family, xlabel, title) in enumerate([
        ('tau_relative', '$\\tau$ Relative-improvement\nThreshold', 'Tau Threshold'),
        ('topk', 'Top-k Replacements\nThreshold', 'Top-k Threshold')]):
        for variant in ['baseline', 'both']:
            rows = policies[(policies.variant == variant) & (policies.rule_family == family)].sort_values('threshold')
            line(axes[row_idx, 0], rows.threshold, rows.recall_at_10, ROUTING[variant])
            line(axes[row_idx, 1], rows.threshold, rows.avg_M, ROUTING[variant])
        for col_idx, ylabel in enumerate(['Recall@10', 'Average PG Probes']):
            ax = axes[row_idx, col_idx]
            ax.set_title(f'({"abcd"[row_idx*2+col_idx]}) {title}')
            grid(ax, xlabel, ylabel)
            if row_idx == 0:
                ax.set_xscale('log')
                ax.set_xticks([.001,.01,.1], labels=['0.001','0.01','0.1'])
                ax.minorticks_off()
            else:
                ax.set_xticks([0,1,2])
    legend(axes[0,0], loc='best')
    save(fig, out, 'lsh_selective_parameters',
         'Offline replay | schedule 8,16,32,64,96 | PG=1024, D=1, 10k queries\nTop-k rules require a defined tau; no policy-specific QPS was measured')


def tree_selective(stages, policies, out):
    """One matched tree-ON dataset: complete M curve and offline policy replays."""
    stages = stages[stages.variant == 'tree_both'].sort_values('M')
    policies = policies[policies.variant == 'tree_both']
    if len(stages) != 5 or len(policies) != 20:
        raise ValueError('Expected all 5 tree stages and 20 policy rows')
    fig, axes = canvas()
    a, b, c, d = axes.flat
    spec = ('LSH + tree', '#1f77b4', 'o')
    line(a, stages.M, 100 * stages.recall_at_stage_mean, spec)
    line(b, stages.M, stages.cum_candidates_mean / 1000, spec)
    a.set_title('(a) Recall vs. Probe Budget')
    b.set_title('(b) Candidates vs. Probe Budget')
    grid(a, 'PG Probe Budget (M)', 'Recall@10 (%)')
    grid(b, 'PG Probe Budget (M)', 'Candidates/query (k)')
    a.set_ylim(60, 100)
    for ax in [a, b]:
        mticks(ax, stages.M)
    for family, spec in TREE_RULES.items():
        rows = policies[policies.rule_family == family].sort_values('avg_M')
        line(c, rows.avg_M, 100 * rows.recall_at_10, spec)
        rows = rows.sort_values('avg_candidates')
        line(d, rows.avg_candidates / 1000, 100 * rows.recall_at_10, spec)
    rows = ideal_curve(pd.read_csv(TREE_RAW))
    line(c, rows.avg_M, 100 * rows.recall_at_10, IDEAL, linestyle='--')
    line(d, rows.avg_candidates / 1000, 100 * rows.recall_at_10, IDEAL, linestyle='--')
    # Past the full-budget oracle point recall cannot rise further, so the
    # ideal stays flat out to the largest fixed budget.
    fixed = policies[policies.rule_family == 'fixed']
    last = rows.iloc[-1]
    for ax, x0, x1 in [(c, last.avg_M, fixed.avg_M.max()),
                       (d, last.avg_candidates / 1000, fixed.avg_candidates.max() / 1000)]:
        ax.plot([x0, x1], [100 * last.recall_at_10] * 2, color=IDEAL[1],
                linestyle='--', linewidth=1.8)
    c.set_title('(c) Selective PG Probing')
    d.set_title('(d) Selective Candidate Cost')
    grid(c, 'Average PG Probes', 'Recall@10 (%)')
    grid(d, 'Candidates/query (k)', 'Recall@10 (%)')
    c.set_xticks([8, 32, 64, 96, 128])
    for ax in [c, d]:
        ax.set_ylim(60, 100)
    legend(c, loc='lower right', fontsize=8)
    save(fig, out, 'lsh_tree_selective_combined',
         'SIFT1M | PG=1024, D=1, 1k queries | Table + margin, tree budget=16\n(c,d): offline replay of recorded stages; oracle requires ground truth')


def tree_parameters(policies, out):
    policies = policies[policies.variant == 'tree_both']
    fig, axes = canvas()
    for i, (family, xlabel) in enumerate([
        ('tau_relative', '$\\tau$ Relative-improvement\nThreshold'),
        ('topk', 'Top-k Replacements\nThreshold')]):
        rows = policies[policies.rule_family == family].sort_values('threshold')
        spec = ('LSH + tree', '#1f77b4', 'o')
        line(axes[i, 0], rows.threshold, rows.recall_at_10 * 100, spec)
        line(axes[i, 1], rows.threshold, rows.avg_M, spec)
        for j, ylabel in enumerate(['Recall@10 (%)', 'Average PG Probes']):
            ax = axes[i, j]
            ax.set_title(f'({"abcd"[2*i+j]}) {"Tau" if i == 0 else "Top-k"} Threshold')
            grid(ax, xlabel, ylabel)
            if i == 0:
                ax.set_xscale('log')
                ax.set_xticks([.001, .01, .1], labels=['0.001','0.01','0.1'])
                ax.minorticks_off()
            else:
                ax.set_xticks([0, 1, 2])
        axes[i, 0].set_ylim(75, 90)
    save(fig, out, 'lsh_tree_selective_parameters',
         'Tree ON | PG=1024, D=1, 1k queries | schedule 8,16,32,64,128\nOffline stopping-rule replay; policy-specific latency/QPS not measured')


def measured(runs, out):
    rows = runs[(runs.family == 'selective') & runs.run.isin([
        'telemetry_m96','rerank_tableonly','rerank_margin','rerank_both'])].copy()
    order = ['telemetry_m96','rerank_tableonly','rerank_margin','rerank_both']
    rows = rows.set_index('run').loc[order]
    fig, axes = canvas()
    for ax, (column, title, ylabel, scale) in zip(axes.flat, [
        ('recall_at_top_k', '(a) Recall', 'Recall@10', 1),
        ('avg_total_matching_records_per_query', '(b) Candidates', 'Candidates/query (k)', 1000),
        ('logical_latency_us_p50', '(c) E2E Latency', 'P50 Latency (ms)', 1000),
        ('logical_qps', '(d) Throughput', 'Queries/second', 1),
    ]):
        ax.bar(range(4), pd.to_numeric(rows[column]) / scale,
               color=[spec[1] for spec in ROUTING.values()], width=.65)
        ax.set_title(title)
        ax.set_xticks(range(4), labels=['Orig.','Table','Margin','Both'])
        grid(ax, 'Routing Order', ylabel)
        ax.set_axisbelow(True)
    save(fig, out, 'lsh_measured_full_schedule',
         'Measured runs: stop=never, every query reaches M=96 | 10k queries\nThese latency/QPS values do not measure early-stopping policies')


def tree_comparison(runs, out):
    fig, axes = canvas()
    arms = [('tree_original','off',('OFF','#7f7f7f','o')),
            ('tree_original','on',('ON','#1f77b4','s')),
            ('tree_original','pruning',('PRUNING','#9467bd','^')),
            ('tree_original','fullpg',('FULLPG','#2ca02c','D')),
            ('tree_fixed','on',('FIXED tree','#ff7f0e','v'))]
    for family, arm, spec in arms:
        rows = runs[(runs.family == family) & runs.run.str.startswith(arm+'_m')].copy()
        rows['m'] = pd.to_numeric(rows.m)
        rows = rows.sort_values('m')
        for ax, col, scale in zip(axes.flat,
                ['recall_at_top_k','avg_total_matching_records_per_query','logical_latency_us_p50','logical_qps'],
                [1,1000,1000,1]):
            line(ax, rows.m, pd.to_numeric(rows[col])/scale, spec)
    for ax, title, ylabel in zip(axes.flat,
        ['(a) Recall','(b) Candidates','(c) E2E Latency','(d) Throughput'],
        ['Recall@10','Candidates/query (k)','P50 Latency (ms)','Queries/second']):
        ax.set_title(title)
        grid(ax,'PG Probe Budget (M)',ylabel)
        mticks(ax,[1,2,4,8,16,32,64,96])
    axes[1,0].set_yscale('log')
    axes[1,1].set_yscale('log')
    legend(axes[0,0], loc='upper left', fontsize=8)
    save(fig, out, 'related_tree_fixed_m',
         'Separate experiment | PG=1024, D=1, 1k queries | Original routing\nFIXED = leaf-crossing fix; all arms use fixed M (not selective stopping)')


def duplication(df, out):
    rows = df[(df['mode'] == 'lsh') & (df.arm == 'ref_c1')]
    fig, axes = canvas()
    for d, spec in [(1,('D=1','#1f77b4','o')), (2,('D=2','#ff7f0e','s')), (3,('D=3','#2ca02c','^'))]:
        selected = rows[rows.duplicate == d].sort_values('m')
        for ax, col, scale in zip(axes.flat,
            ['recall_at_top_k','candidates_per_query','latency_us_avg','logical_qps'], [1,1000,1000,1]):
            line(ax,selected.m,selected[col]/scale,spec)
    for ax, title, ylabel in zip(axes.flat,
        ['(a) Recall','(b) Candidates','(c) E2E Latency','(d) Throughput'],
        ['Recall@10','Candidates/query (k)','Mean Latency (ms)','Queries/second']):
        ax.set_title(title)
        grid(ax,'PG Probe Budget (M)',ylabel)
        mticks(ax,[1,8,16,32])
    legend(axes[0,0])
    save(fig,out,'related_replication_20260909',
         '2026-09-09 | LSH + tree, 150 queries, concurrency=1\nquery-table-count=D; tree budget=64, patience=4, onode budget=64')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=HERE/'tables')
    parser.add_argument('--output-dir',type=Path,default=HERE/'figures')
    parser.add_argument('--include-related',action='store_true',help='Also plot separate tree and September D experiments')
    parser.add_argument('--include-diagnostics',action='store_true',help='Also plot the earlier single-bucket/tree-OFF diagnostic data')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    stages = pd.read_csv(args.data_dir/'stage_summary.csv')
    policies = pd.read_csv(args.data_dir/'selective_policies.csv')
    runs = pd.read_csv(args.data_dir/'measured_runs_all_fields.csv')
    style()
    tree_selective(stages,policies,args.output_dir)
    tree_parameters(policies,args.output_dir)
    if args.include_diagnostics:
        combined(stages,policies,args.output_dir)
        parameters(policies,args.output_dir)
        measured(runs,args.output_dir)
    if args.include_related:
        tree_comparison(runs,args.output_dir)
        duplication(pd.read_csv(args.data_dir/'duplication_all_columns.csv'),args.output_dir)


if __name__ == '__main__':
    main()
