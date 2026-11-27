#!/usr/bin/env python3
"""Deep1B 1B inputs: first 1000 public queries -> fvecs, official 1B GT
(deep_new_groundtruth.public.10K.bin: u32 n, u32 k, n*k u32 ids, n*k f32
dists) -> ivecs top-100 for those queries, and checks:
  * base header = 1e9 x 96 and file size exact
  * the queries equal the 1000 used for 1M/10M/100M
  * for 20 queries, every GT top-10 distance recomputed from base rows matches
    the GT file and is ascending
  * for 20 queries, the 1B GT neighbours with id < 1e8 appear, in order, in
    the 100M GT (a prefix of the same base)
usage: 00_prep.py <deep1b dir> <out dir>"""
import struct
import sys
import numpy as np

src, out = sys.argv[1], sys.argv[2]
Q = 1000
base_path = f'{src}/base.1B.fbin'
n, d = struct.unpack('<II', open(base_path, 'rb').read(8))
import os
size = os.path.getsize(base_path)
assert (n, d) == (1000000000, 96) and size == 8 + n * d * 4, (n, d, size)
base = np.memmap(base_path, dtype=np.float32, mode='r', offset=8, shape=(n, d))

qn, qd = struct.unpack('<II', open(f'{src}/query.public.10K.fbin', 'rb').read(8))
q = np.fromfile(f'{src}/query.public.10K.fbin', dtype=np.float32, offset=8).reshape(qn, qd)[:Q]
old = np.fromfile('/home/dev_path/datasets/deep1b10m/deep1b10m_query.fvecs', dtype=np.float32).reshape(-1, d + 1)[:, 1:]
assert np.array_equal(q, old[:Q]), 'query mismatch vs 10M/100M runs'
with open(f'{out}/deep1b_query1000.fvecs', 'wb') as f:
    np.hstack([np.full((Q, 1), d, dtype=np.int32).view(np.float32), q]).astype(np.float32).tofile(f)

raw = open(f'{src}/deep_new_groundtruth.public.10K.bin', 'rb').read()
gn, gk = struct.unpack('<II', raw[:8])
ids = np.frombuffer(raw, dtype=np.uint32, count=gn * gk, offset=8).reshape(gn, gk)[:Q]
dist = np.frombuffer(raw, dtype=np.float32, count=gn * gk, offset=8 + gn * gk * 4).reshape(gn, gk)[:Q]
with open(f'{out}/deep1b_gt1000.ivecs', 'wb') as f:
    np.hstack([np.full((Q, 1), gk, dtype=np.int32), ids.astype(np.int32)]).tofile(f)

for qi in range(0, Q, 50):
    v = base[ids[qi, :10]]
    dd = ((v - q[qi]) ** 2).sum(1)
    assert np.allclose(dd, dist[qi, :10], rtol=1e-4, atol=1e-4), (qi, dd, dist[qi, :10])
    assert np.all(np.diff(dist[qi, :10]) >= 0)
g100 = np.fromfile('/home/dev_path/experiments/workload-scale-sq8/groundtruth/deep100m_gt_q1000.ivecs',
                   dtype=np.int32)
k100 = g100[0]
g100 = g100.reshape(-1, k100 + 1)[:, 1:]
for qi in range(0, Q, 50):
    sub = [int(x) for x in ids[qi] if x < 100000000]
    m = min(len(sub), k100)
    assert list(g100[qi, :m]) == sub[:m] or set(g100[qi, :min(10, m)]) == set(sub[:min(10, m)]), qi
print(f'OK base {n}x{d}, queries {Q} (== 100M run), GT {gn}x{gk} -> top-{gk} for {Q}; '
      'distance + 100M-prefix checks passed')
