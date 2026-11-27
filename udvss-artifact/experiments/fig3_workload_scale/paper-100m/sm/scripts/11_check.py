#!/usr/bin/env python3
"""Checks the 10_correctness.sh census + `ceph osd map` output.

Usage: 11_check.py [results/correctness]
Writes mapping_sample.tsv and prints one PASS/FAIL line per check.
"""
import csv
import sys
from pathlib import Path

R = Path(sys.argv[1] if len(sys.argv) > 1 else
         '/home/dev_path/experiments/paper-100m/sm/results/correctness')
K = 10
PGS = [128, 256, 512, 1024]
STAGES = {f'split{n}': n for n in PGS} | {f'merge{n}': n for n in PGS} | \
         {'fresh256': 256, 'fresh512': 512}
FRESH = {128: 'split128', 256: 'fresh256', 512: 'fresh512', 1024: 'merge1024'}


def census(tag):
    lines = (R / f'{tag}.census.csv').read_text().splitlines()
    lines = lines[next(i for i, l in enumerate(lines) if l.startswith('oid,')):]
    return {(int(r['partition']), r['sub_oid']): r for r in csv.DictReader(lines)}


def osdmap(tag):
    out = {}
    for line in (R / f'{tag}.osdmap').read_text().splitlines():
        key, raw, pg = line.split()
        out[key] = (int(raw, 16), int(pg, 16))
    return out


def total(tag):
    line = (R / f'{tag}.census.err').read_text().split('CENSUS_TOTAL', 1)[1]
    return dict(kv.split('=') for kv in line.split())


results = []


def check(name, bad, detail=''):
    results.append((name, not bad))
    print(f"{'PASS' if not bad else 'FAIL'}  {name}"
          f"{'' if not bad else f'  ({len(bad)} bad, e.g. {bad[:3]})'} {detail}")


tags = [t for t in STAGES if (R / f'{t}.census.csv').exists()]
C = {t: census(t) for t in tags}
M = {t: osdmap(t) for t in tags if (R / f'{t}.osdmap').exists()}
print(f'stages present: {tags}')

# 1. Every object was read where the client routed it; nothing lost.
bad = []
for t in tags:
    tot = total(t)
    if tot['missing'] != '0' or tot['failed'] != '0' or tot['records'] != '20000':
        bad.append((t, tot))
    bad += [(t, k) for k, r in C[t].items() if r['r'] != '0' or r['op_rval'] != '0']
check('every object read at its routed PG (missing=failed=0, 20000 records)', bad)

# 2. All g/r sub-objects of a bucket share one locator and one PG.
bad = []
for t in tags:
    pg, loc = {}, {}
    for (b, _), r in C[t].items():
        pg.setdefault(b, set()).add(r['phys_pg'])
        loc.setdefault(b, set()).add(r['locator'])
    bad += [(t, b) for b in pg if len(pg[b]) != 1 or len(loc[b]) != 1]
check('same bucket -> same locator and same PG for all sub-objects', bad)

# 3. PG = low log2(pg_num) bits of the LSH bucket id (table 0: ordinal = id).
bad = [(t, k, r['phys_pg']) for t in tags for k, r in C[t].items()
       if k[0] >= (1 << K) or int(r['phys_pg']) != (k[0] & (STAGES[t] - 1))]
check('PG == lsh_bucket_id & (pg_num - 1)', bad)

# 4. The monitor agrees: its raw hash carries the bucket id in the low k
#    bits and its PG equals the client's.
bad = []
for t, m in M.items():
    client = {r['locator']: int(r['phys_pg']) for r in C[t].values()}
    bucket = {r['locator']: int(r['partition']) for r in C[t].values()}
    for key, (raw, pg) in m.items():
        if raw & ((1 << K) - 1) != bucket[key] or pg != client[key] or \
           pg != raw & (STAGES[t] - 1):
            bad.append((t, key, hex(raw), pg, client[key]))
    if set(m) != set(client):
        bad.append((t, 'key sets differ'))
check('ceph osd map: raw & (2^k-1) == bucket, PG == client PG', bad,
      f'({sum(len(m) for m in M.values())} locator lookups)')


def bucket_pg(t):
    return {b: int(r['phys_pg']) for (b, _), r in C[t].items()}


# 5. Split N -> 2N: child = parent + bit_log2(N) * N.
bad = []
for n in PGS[:-1]:
    a, b = f'split{n}', f'split{2 * n}'
    if a in C and b in C:
        pa, pb = bucket_pg(a), bucket_pg(b)
        bad += [(n, bk) for bk in pa
                if pb[bk] != pa[bk] + ((bk >> (n.bit_length() - 1)) & 1) * n]
check('split N->2N adds exactly LSH bit log2(N)', bad)

# 6. Merge 2N -> N: parent = child & (N-1), and the PGs joined are exactly
#    {p, p + N}: buckets that differ only in LSH bit log2(N).
bad = []
for n in PGS[:-1]:
    a, b = f'merge{2 * n}', f'merge{n}'
    if a in C and b in C:
        pa, pb = bucket_pg(a), bucket_pg(b)
        bad += [(n, bk) for bk in pa if pb[bk] != pa[bk] & (n - 1)]
        joined = {}
        for bk in pa:
            joined.setdefault(pb[bk], set()).add(pa[bk])
        bad += [(n, p, sorted(s)) for p, s in joined.items()
                if not s <= {p, p + n}]
check('merge 2N->N drops LSH bit log2(N); joins only PGs p and p+N', bad)

# 7. Path independence: a pool split or merged to pg_num N maps every
#    object exactly like a fresh load at N (same objects, PGs, records).
bad = []
for kind in ('split', 'merge'):
    for n in PGS:
        t, f = f'{kind}{n}', FRESH[n]
        if t == f or t not in C or f not in C:
            continue
        if set(C[t]) != set(C[f]):
            bad.append((t, f, 'object sets differ'))
            continue
        bad += [(t, f, k) for k in C[t]
                if (C[t][k]['phys_pg'], C[t][k]['records'], C[t][k]['locator'])
                != (C[f][k]['phys_pg'], C[f][k]['records'], C[f][k]['locator'])]
check('split/merge result == fresh load at the same pg_num (per object)', bad)

# Mapping sample for the report.
if all(f'split{n}' in C for n in PGS):
    P = {n: bucket_pg(f'split{n}') for n in PGS}
    with open(R / 'mapping_sample.tsv', 'w') as out:
        out.write('bucket\tbits(9..0)\tpg128\tpg256\tpg512\tpg1024\n')
        for bk in sorted(P[128])[::max(1, len(P[128]) // 24)]:
            out.write(f'{bk}\t{bk:010b}\t' +
                      '\t'.join(str(P[n][bk]) for n in PGS) + '\n')
    print(f'wrote {R / "mapping_sample.tsv"}')

print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed")
sys.exit(0 if all(ok for _, ok in results) else 1)
