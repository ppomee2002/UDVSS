"""Client-side reimplementation of pg-lsh-v0 routing, for offline analysis.

Mirrors librados/vector_placement.h exactly:
  mix_u32, pg_lsh_v0_hyperplane_sign, pg_lsh_v0_lsh_bucket_id,
  pg_lsh_v0_group_to_pg, pg_lsh_v0_query_groups, pg_lsh_v0_select_unique_pgs.
"""
import itertools
import numpy as np

M32 = 0xFFFFFFFF


def mix_u32(v):
    v &= M32
    v ^= v >> 16; v = (v * 0x7feb352d) & M32
    v ^= v >> 15; v = (v * 0x846ca68b) & M32
    v ^= v >> 16
    return v


def hyperplane_matrix(seed, table, k, dim):
    """signs[bit][dim], matching pg_lsh_v0_hyperplane_sign()."""
    bits = np.arange(k, dtype=np.uint64)[:, None]
    dims = np.arange(dim, dtype=np.uint64)[None, :]
    mixed = (np.uint64(seed) ^ (np.uint64(table) * np.uint64(0x9e3779b9))
             ^ (bits * np.uint64(0x85ebca6b)) ^ (dims * np.uint64(0xc2b2ae35)))
    m = mixed & np.uint64(M32)
    m ^= m >> np.uint64(16); m = (m * np.uint64(0x7feb352d)) & np.uint64(M32)
    m ^= m >> np.uint64(15); m = (m * np.uint64(0x846ca68b)) & np.uint64(M32)
    m ^= m >> np.uint64(16)
    return np.where((m & np.uint64(1)) == 0, -1.0, 1.0)


def projections(vec, seed, table, k):
    """Raw per-bit projections; sign gives the bucket bit, |value| the margin."""
    return hyperplane_matrix(seed, table, k, len(vec)) @ vec.astype(np.float64)


def bucket_id(proj):
    bits = (proj >= 0).astype(np.uint32)
    return int(np.sum(bits << np.arange(len(bits), dtype=np.uint32)))


def group_to_pg(table, bucket, pg_num, seed):
    return mix_u32(seed ^ ((table * 0x9e3779b9) & M32)
                   ^ ((bucket * 0x85ebca6b) & M32)) % pg_num


def masks_at_distance(k, d):
    if d == 0:
        return [0]
    return [sum(1 << b for b in combo) for combo in itertools.combinations(range(k), d)]


def query_groups(vec, seed, k, l, radius):
    """(table, bucket, distance, mask) in the exact order the C++ emits."""
    exact = []
    for t in range(l):
        p = projections(vec, seed, t, k)
        exact.append((t, bucket_id(p), p))
    out = []
    for d in range(radius + 1):
        masks = masks_at_distance(k, d)
        for t, b, _p in exact:
            for m in masks:
                out.append((t, b ^ m, d, m))
    return out, exact


def select_unique_pgs(groups, pg_num, seed, budget):
    """Returns [(pg, table, distance, mask)] in rank order."""
    seen, out = set(), []
    for t, b, d, m in groups:
        pg = group_to_pg(t, b, pg_num, seed)
        if pg in seen:
            continue
        seen.add(pg)
        out.append((pg, t, d, m))
        if len(out) == budget:
            break
    return out
