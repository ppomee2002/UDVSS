#!/usr/bin/env python3
"""old (pg-objloc-1m bucket1m, hashed bucket locators) vs new (LSH-aligned)
for the split_merge_combined figure points. Writes results/splitmerge/
summary_old_vs_new.csv."""
import re, csv
from pathlib import Path
NEW = Path('/home/dev_path/experiments/lsh-aligned-1m/results/splitmerge')
OLD = Path('/home/dev_path/experiments/pg-objloc-1m/results/bucket1m')
F = ('recall_at_top_k', 'avg_total_matching_records_per_query', 'failures',
     'missing_objects', 'logical_latency_us_avg', 'logical_latency_us_p99')
def parse(path, tags):
    out = {}
    for line in open(path):
        tag = line.split()[0]
        if tag not in tags: continue
        m = int(re.search(r' m=(\d+) ', line).group(1))
        kv = dict(re.findall(r'(\w+)=([^ ]+)', line))
        out[(tags[tag], m)] = {k: float(kv[k]) for k in F if k in kv}
    return out
P = [128, 256, 512, 1024]
new = {}
new |= {('split',) + k: v for k, v in parse(NEW/'raw.txt', {f'asplit_pg{n}': n for n in P}).items()}
new |= {('merge',) + k: v for k, v in parse(NEW/'raw.txt', {f'amerge_pg{n}': n for n in P}).items()}
new |= {('fullscan',) + k: v for k, v in parse(NEW/'raw_fullscan.txt', {f'famerge_pg{n}': n for n in P}).items()}
old = {}
old |= {('split',) + k: v for k, v in parse(OLD/'raw.txt', {f'bsplit_pg{n}': n for n in P}).items()}
old |= {('merge',) + k: v for k, v in parse(OLD/'raw_merge3.txt', {f'bmerge3_pg{n}': n for n in P}).items()}
old |= {('fullscan',) + k: v for k, v in parse(OLD/'raw_fullscan.txt', {f'fbmerge4_pg{n}': n for n in P}).items()}
rows = []
for arm, ms in (('split', (16, 32, 64)), ('merge', (16, 32, 64)), ('fullscan', (16,))):
    for m in ms:
        for n in (P if arm == 'split' else P[::-1]):
            o, w = old.get((arm, n, m)), new.get((arm, n, m))
            rows.append(dict(arm=arm, M=m, pg_num=n,
                old_recall=o and o['recall_at_top_k'], new_recall=w['recall_at_top_k'],
                old_cand=o and o['avg_total_matching_records_per_query'],
                new_cand=w['avg_total_matching_records_per_query'],
                old_lat_ms=o and o.get('logical_latency_us_avg', 0) / 1e3,
                new_lat_ms=w.get('logical_latency_us_avg', 0) / 1e3,
                new_failures=w.get('failures'), new_missing=w.get('missing_objects', 0)))
with open(NEW/'summary_old_vs_new.csv', 'w') as f:
    wr = csv.DictWriter(f, rows[0].keys()); wr.writeheader(); wr.writerows(rows)
tot_fail = sum(v.get('failures', 0) + v.get('missing_objects', 0) for v in new.values())
print(f'new: {len(new)} points, failures+missing total = {tot_fail:.0f}')
for arm in ('split', 'merge', 'fullscan'):
    print(f'\n[{arm}] recall / candidates   old -> new')
    for r in rows:
        if r['arm'] != arm: continue
        o = f"{r['old_recall']:.3f} / {r['old_cand']/1e3:6.1f}k" if r['old_recall'] is not None else '   n/a'
        print(f"  M={r['M']:<3} pg={r['pg_num']:<5} {o}  ->  {r['new_recall']:.3f} / {r['new_cand']/1e3:6.1f}k"
              f"   (Δrecall {r['new_recall']-(r['old_recall'] or 0):+.3f})")
