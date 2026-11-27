#!/usr/bin/env python3
"""Read saved results only; export complete source tables and plotting summaries.

No benchmark, subprocess, cluster connection, or source-file modification.
Requires pandas/numpy. Unknown values are empty CSV cells, never zero-filled.
"""
import argparse
import csv
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
EXP = Path('/home/dev_path/experiments')
SEL = EXP / 'selective-fanout-sift1m'
ROUTING = EXP / 'routing-diagnostics-sift1m'
TREE = EXP / 'vectornode-tree-boundary-1m'
DUP = EXP / 'dup-fanout'
SCRATCH = Path('/tmp/udvss-scratch/boundary-search')
SESSION = Path('/tmp/udvss-scratch/session.jsonl')
VARIANTS = {
    'telemetry_m96': ('baseline', 'lsh_only', 0, False),
    'rerank_tableonly': ('table_only', 'lsh_only', 1, False),
    'rerank_margin': ('margin_only', 'lsh_only', 0, True),
    'rerank_both': ('both', 'lsh_only', 1, True),
    'equivalence_m64': ('equivalence', 'lsh_only', 0, False),
    'tree_msweep': ('tree_both', 'lsh_tree', 1, True),
}


def write_table(df, path):
    df.to_csv(path.with_suffix('.csv'), index=False)
    df.to_csv(path.with_suffix('.tsv'), index=False, sep='\t')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def summarize(chosen, final_recall):
    result = {
        'query_count': len(chosen),
        'recall_at_10': chosen.recall_at_stage.mean(),
        'avg_M': chosen.cum_m.mean(),
        'avg_actual_pg_probes': chosen.cum_m.mean(),
        'avg_stages': chosen.stage.mean() + 1,
        'avg_candidates': chosen.cum_candidates.mean(),
        'avg_distance_computations': chosen.cum_distcomp.mean(),
        'vs_max_recall_pct': 100 * chosen.recall_at_stage.mean() / final_recall,
        # These are observations in the never-stop run, NOT measured early-stop latency.
        'observed_prefix_latency_us_avg': chosen.cum_latency_us.mean(),
        'observed_prefix_latency_us_p50': chosen.cum_latency_us.median(),
        'observed_prefix_latency_us_p95': chosen.cum_latency_us.quantile(.95),
        'policy_measured_qps': np.nan,
        'policy_measured_latency_us_avg': np.nan,
        'final_m_histogram': json.dumps({str(int(k)): int(v) for k, v in chosen.cum_m.value_counts().sort_index().items()}),
    }
    return result


def policy_table(df, metadata):
    """Reproduce recorded stopping rules, plus the same thresholds for each routing arm."""
    df = df.sort_values(['query_index', 'stage'])
    last = int(df.stage.max())
    last_rows = df[df.stage == last]
    max_recall = last_rows.recall_at_stage.mean()
    rows = []

    def add(family, threshold, mask, name, floor=None):
        chosen = df[mask | (df.stage == last)].drop_duplicates('query_index', keep='first')
        if len(chosen) != df.query_index.nunique():
            raise ValueError('Incomplete query selection')
        rows.append({**metadata, 'evaluation': 'offline_replay_of_saved_stages',
                     'rule_family': family, 'threshold': threshold, 'rule': name,
                     'minimum_stop_M': floor, **summarize(chosen, max_recall)})

    for m in sorted(df.cum_m.unique()):
        add('fixed', int(m), df.cum_m >= m, f'fixed M={m}')
    for t in [.001, .005, .01, .02, .05, .1]:
        add('tau_relative', t, df.tau.notna() & (df.tau_impr_rel < t), f'tau rel-improvement < {t}')
    add('topk_unqualified', 0, df.topk_replacements == 0, 'topk_replacements == 0')
    for k in [0, 1, 2]:
        add('topk', k, df.tau.notna() & (df.topk_replacements <= k), f'topk_replacements <= {k} AND tau set')
    for t in [1.05, 1.1, 1.2, 1.5]:
        add('tau_over_d1', t, df.tau_over_d1 < t, f'tau/d1 < {t}')
    final = df.query_index.map(last_rows.set_index('query_index').recall_at_stage)
    add('oracle', np.nan, df.recall_at_stage >= final, 'ORACLE (requires ground truth)')
    if metadata['variant'] == 'baseline':
        add('floor64_topk', 0, (df.cum_m >= 64) & (df.topk_replacements == 0), 'floor 64, then topk_repl==0', 64)
        for t in [.001, .01, .05]:
            add('floor64_tau', t, (df.cum_m >= 64) & (df.tau_impr_rel < t), f'floor 64, then tau rel-impr < {t}', 64)
    return rows


def session_text(obj):
    content = obj.get('message', {}).get('content', [])
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        val = block.get('text') or block.get('content')
        if isinstance(val, str):
            parts.append(val)
    return '\n'.join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=HERE)
    args = parser.parse_args()
    out = args.output_dir.resolve()
    for source in [EXP, Path('/tmp/x'), SCRATCH, SESSION.parent]:
        if out == source or source in out.parents or out in source.parents:
            raise ValueError(f'Output must be separate from original data: {source}')
    tables = out / 'tables'
    tables.mkdir(parents=True, exist_ok=True)
    archive = out / 'raw'
    manifest = []
    archived = set()

    def snapshot(path, kind):
        path = Path(path)
        if path in archived:
            return
        if not path.is_file():
            raise FileNotFoundError(path)
        before = digest(path)
        dest = archive / path.relative_to('/')
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        after = digest(dest)
        if before != after:
            raise ValueError(f'Archive hash mismatch: {path}')
        manifest.append({'source_path': str(path), 'archived_path': str(dest.relative_to(out)),
                         'source_modified_utc': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                         'bytes': path.stat().st_size, 'sha256': before, 'kind': kind})
        archived.add(path)

    stage_rows, policies, per_query = [], [], []
    for stem, (variant, classification, table_count, margin) in VARIANTS.items():
        path = SEL / 'results' / f'{stem}.csv'
        snapshot(path, 'complete_per_query_stage_csv')
        df = pd.read_csv(path)
        nqueries = df.query_index.nunique()
        schedule = sorted(df.cum_m.unique())
        if df.duplicated(['query_index', 'stage']).any() or len(df) != nqueries * len(schedule):
            raise ValueError(f'Incomplete/duplicate stage data: {path}')
        expected_batches = np.diff([0, *schedule])
        for stage, group in df.groupby('stage'):
            if not (group.batch_pg_count == expected_batches[stage]).all():
                raise ValueError(f'Unexpected actual PG counts: {path}')
        meta = dict(variant=variant, classification=classification, D=1, pg_num=1024,
                    initial_M=int(schedule[0]), max_M=int(schedule[-1]),
                    schedule=','.join(map(str, schedule)), query_table_count=table_count,
                    margin_ordered_probes=margin, hamming_radius=10, top_k=10,
                    lsh_k=10, lsh_L=4, seed=1315423911, query_concurrency=32,
                    base_vectors=1000000, pool='sift1m_raw_v1024',
                    distance_bucket_bits=4, residual_bits=4,
                    distance_bucket_radius=0, residual_hamming_radius=0,
                    probe_limit_per_pg=1, raw_euclidean_geometry=True,
                    boundary_search=False,
                    tree_search_budget=16 if classification == 'lsh_tree' else 0,
                    source_csv=str(path), measurement_stop='never')
        for m, group in df.groupby('cum_m', sort=True):
            row = {**meta, 'M': int(m), 'query_count': len(group),
                   'actual_avg_pg_probes': group.cum_m.mean()}
            for col in df.columns:
                if col not in ('query_index', 'stage', 'cum_m'):
                    row[f'{col}_mean'] = group[col].mean()
            for col in ['batch_latency_us', 'cum_latency_us']:
                for quantile in [.5, .95, .99]:
                    row[f'{col}_p{int(100*quantile)}'] = group[col].quantile(quantile)
            row['tau_defined_queries'] = group.tau.notna().sum()
            stage_rows.append(row)
        enriched = df.copy()
        for key, value in meta.items():
            enriched[key] = value
        per_query.append(enriched)
        if variant != 'equivalence':
            policies.extend(policy_table(df, meta))
    write_table(pd.DataFrame(stage_rows), tables / 'stage_summary')
    write_table(pd.DataFrame(policies), tables / 'selective_policies')
    write_table(pd.DataFrame(stage_rows).query("variant == 'tree_both'"), tables / 'tree_selective_stages')
    write_table(pd.DataFrame(policies).query("variant == 'tree_both'"), tables / 'tree_selective_policies')
    write_table(pd.concat(per_query, ignore_index=True), tables / 'per_query_stages_all')

    # Preserve ALL CONFIG/WARMUP/FANOUT_QUERY key-value fields, including tree counters.
    runs, records = [], []
    groups = [
        ('selective', sorted((SEL / 'logs').glob('*.log'))),
        ('routing_fixed', sorted((ROUTING / 'logs/msweep').glob('m*.log'))),
        ('tree_original', sorted(Path('/tmp/x/raw_v1024_results').glob('*.log'))),
        ('tree_fixed', sorted((TREE / 'results/05_msweep').glob('*.log'))),
        ('duplication', sorted((DUP / 'logs').glob('q_*.log'))),
    ]
    for family, paths in groups:
        for path in paths:
            snapshot(path, f'{family}_log')
            config = {}
            for line_num, line in enumerate(path.read_text().splitlines(), 1):
                record_type = line.split(' ', 1)[0]
                if record_type not in ['CONFIG', 'WARMUP', 'FANOUT_QUERY']:
                    continue
                fields = dict(re.findall(r'(\w+)=(\S+)', line))
                records.append(dict(family=family, source_log=str(path), source_line=line_num,
                                    record_type=record_type, **fields))
                if record_type == 'CONFIG':
                    config = fields
                if record_type == 'FANOUT_QUERY':
                    overlap = set(config) & set(fields)
                    if overlap:
                        raise ValueError(f'CONFIG/run key collision: {overlap}')
                    classification = ('pivot' if fields.get('routing_mode', config.get('routing_mode')) == 'pivot'
                                      or 'q_pivot_' in path.name else
                                      'lsh_tree' if any(k.startswith('tree_') for k in fields) else 'lsh_only')
                    runs.append(dict(family=family, run=path.stem, classification=classification,
                                     source_log=str(path), source_line=line_num, **config, **fields))
    write_table(pd.DataFrame(runs), tables / 'measured_runs_all_fields')
    write_table(pd.DataFrame(records), tables / 'log_records_all_fields')

    for name in ['msweep_summary.csv', 'per_query_score_vs_M.csv', 'diagnose.jsonl']:
        snapshot(ROUTING / 'results' / name, 'routing_diagnostic')
    for name in ['msweep_summary.csv', 'per_query_score_vs_M.csv']:
        write_table(pd.read_csv(ROUTING / 'results' / name), tables / f'routing_{Path(name).stem}')
    dup_rows = []
    for path in sorted((DUP / 'results').glob('*_d[123].csv')):
        if path.name.startswith(('lsh_', 'pivot_')):
            snapshot(path, 'duplication_original_csv')
            df = pd.read_csv(path)
            df['source_csv'] = str(path)
            df['experiment_date'] = '2026-09-09'
            df['classification'] = 'lsh_tree' if path.name.startswith('lsh') else 'pivot'
            dup_rows.append(df)
    write_table(pd.concat(dup_rows, ignore_index=True), tables / 'duplication_all_columns')

    # Every Markdown table in the relevant reports, without dropping or rounding cells.
    report_paths = [SEL / 'results/report.md', ROUTING / 'results/report.md',
                    TREE / 'results/SUMMARY_phase5_msweep.md', DUP / 'REPORT.md']
    table_index = []
    for report_num, path in enumerate(report_paths, 1):
        snapshot(path, 'original_report')
        blocks, block = [], []
        for line_num, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith('|'):
                block.append((line_num, line))
            elif block:
                blocks.append(block)
                block = []
        if block:
            blocks.append(block)
        for number, block in enumerate(blocks, 1):
            cells = [[s.strip() for s in line.strip().strip('|').split('|')] for _, line in block]
            if len(cells) < 2 or not all(re.fullmatch(r':?-+:?', s) for s in cells[1]):
                continue
            name = f'report_{report_num:02}_table_{number:02}'
            for extension, delimiter in [('csv', ','), ('tsv', '\t')]:
                with (tables / f'{name}.{extension}').open('w', newline='') as f:
                    writer = csv.writer(f, delimiter=delimiter)
                    writer.writerow(cells[0])
                    writer.writerows(cells[2:])
            table_index.append(dict(table=name, source_path=str(path), source_line=block[0][0],
                                    rows=len(cells)-2, columns=len(cells[0])))
    write_table(pd.DataFrame(table_index), tables / 'report_tables_index')

    # Original historical stdout + its generating commands (read-only provenance).
    wanted = {812, 849, 850, 859, 860, 1120, 2603, 2669}
    excerpts = {}
    for n, line in enumerate(SESSION.open(), 1):
        if n in wanted:
            obj = json.loads(line)
            excerpts[n] = obj
    if set(excerpts) != wanted:
        raise ValueError('Historical source lines missing')
    history_dir = out / 'historical'
    history_dir.mkdir(exist_ok=True)
    for n, obj in excerpts.items():
        (history_dir / f'session_line_{n}.json').write_text(json.dumps(obj, ensure_ascii=False, indent=2))
        (history_dir / f'session_line_{n}.txt').write_text(session_text(obj))
    manifest.append(dict(source_path=str(SESSION), archived_path='historical/session_line_*.json',
                         source_modified_utc=datetime.fromtimestamp(SESSION.stat().st_mtime, timezone.utc).isoformat(),
                         bytes=SESSION.stat().st_size, sha256=digest(SESSION), kind='selected_session_records'))

    # Historical 19-row table, preserving the original rounded values and all columns.
    text = session_text(excerpts[850])
    historical = []
    pattern = r'^(.+?)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9]+)\s+([0-9.]+)%$'
    for line in text.splitlines():
        match = re.match(pattern, line.strip())
        if match:
            historical.append(match.groups())
    if len(historical) != 19:
        raise ValueError(f'Expected 19 historical rows, got {len(historical)}')
    h = pd.DataFrame(historical, columns=['rule','recall@10','avg_M','avg_stg','avg_cand','vs M=96 (%)'])
    write_table(h, tables / 'historical_selective_19rows')
    for n, name in [(860, 'historical_floor64_and_skip'), (1120, 'historical_routing_before_after')]:
        rows, section = [], ''
        for line in session_text(excerpts[n]).splitlines():
            if line.startswith(('--- stage', '--- dead', '############')):
                section = line.strip()
            match = re.match(r'^(.+?)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)%$', line.strip())
            if match:
                rows.append((section, *match.groups()))
        write_table(pd.DataFrame(rows, columns=['section','rule','recall@10','avg_M','avg_stg','vs max (%)']), tables / name)

    # The remembered OFF/ON/FULLPG/PRUNING table, all 13 original columns.
    tree_text = session_text(excerpts[2603]).splitlines()
    tree_header = re.split(r'\s{2,}', tree_text[0].strip())
    tree_data = []
    for line in tree_text[1:]:
        if re.match(r'^\d+\s', line):
            fields = re.split(r'\s{2,}', line.strip())
            # The last printed cell is directly followed by the user's request.
            fields[-1] = re.match(r'[\d.]+ms', fields[-1]).group()
            tree_data.append(fields)
    if len(tree_header) != 13 or len(tree_data) != 8:
        raise ValueError('Historical tree table shape changed')
    write_table(pd.DataFrame(tree_data, columns=tree_header), tables / 'historical_tree_13columns')

    # Check the reaggregation against the historical printed table, including rounding.
    baseline = pd.DataFrame(policies).query("variant == 'baseline'")
    order = ([('fixed', 96)] + [('fixed', m) for m in [8,16,32,64]] +
             [('tau_relative', t) for t in [.001,.005,.01,.02,.05,.1]] +
             [('topk_unqualified', 0), ('topk', 0), ('topk', 1), ('topk', 2)] +
             [('tau_over_d1', t) for t in [1.05,1.1,1.2,1.5]])
    for hist, (family, threshold) in zip(historical, order):
        r = baseline[(baseline.rule_family == family) & (baseline.threshold == threshold)].iloc[0]
        observed = [f'{r.recall_at_10:.5f}',f'{r.avg_M:.1f}',f'{r.avg_stages:.2f}',f'{r.avg_candidates:.0f}',f'{r.vs_max_recall_pct:.1f}']
        if observed != list(hist[1:]):
            raise ValueError(f'Historical mismatch {hist[0]}: {observed} != {hist[1:]}')

    scripts = [SEL / 'scripts/analyze_selective.py', SEL / 'scripts/routing_model.py',
               SCRATCH / 'common_args.txt', SCRATCH / 'recon_args.txt',
               SCRATCH / 'sweep_rerank.sh', SCRATCH / 'sweep_m.sh', SCRATCH / 'sweep_inner.sh', SCRATCH / 'sweep_ceiling.sh',
               ROUTING / 'scripts/run_msweep.sh', ROUTING / 'scripts/build_msweep_csv.py',
               TREE / 'scripts/05_msweep_fixed.sh', TREE / 'scripts/env.sh',
               Path('/tmp/x/run_raw_v1024_sweep.sh'), Path('/tmp/x/raw_v1024_sweep_stdout.log'),
               DUP / 'scripts/measure.sh', DUP / 'scripts/env.sh', DUP / 'scripts/run_all.sh',
               SEL / 'results/selective-fanout-findings.html']
    for path in scripts:
        snapshot(path, 'provenance_only_do_not_execute')
    write_table(pd.DataFrame(manifest), out / 'source_manifest')
    checks = {'historical_19_rows_match': True, 'source_stage_rows': sum(len(d) for d in per_query),
              'stage_summary_rows': len(stage_rows), 'policy_rows': len(policies),
              'measured_run_rows': len(runs), 'markdown_tables': len(table_index),
              'snapshot_hashes_match': True, 'benchmark_executed': False}
    (out / 'validation.json').write_text(json.dumps(checks, indent=2) + '\n')
    print(json.dumps(checks, indent=2))
    print(f'Tables: {tables}')


if __name__ == '__main__':
    main()
