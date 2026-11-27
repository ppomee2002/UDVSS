// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_COMMON_VECTOR_PIVOT_TREE_H
#define CEPH_COMMON_VECTOR_PIVOT_TREE_H

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <queue>
#include <vector>

// Training-derived bit partitioning for the PG-LSH bucket id
// (placement_mode_training_derived in librados/vector_pg_lsh.h): the same
// objects, sub-OIDs, locators and split/merge path as hierarchical_lsh,
// with only the per-level bit rule replaced.
//
// Every region R (the bit prefix decided so far) has a trained pivot p_R
// and a distance boundary r_R:
//
//   b_R = d(v, p_R) < r_R ? 0 : 1        (d = Euclidean distance)
//
// Starting from the root region, one bit is generated per level and the
// next region is R b_R. After L levels
//
//   PGValue(v) = sum_{l=1..L} b_l 2^(l-1)
//
// so b_1 is the LSB and value & (2^n - 1) is the region at depth n, the
// same bit order as vector_pg_lsh_hierarchical.h.
//
// Training (build()) is a vantage-point split of a sample: for each region
// a few member samples are tried as the pivot, the one whose distances to
// the region's members have the largest variance is kept, and r_R is the
// median of the members' distances to it, so each region is halved.
// Everything is deterministic in (samples, levels, seed).
//
// Query multi-probe goes through descend() with a flip mask, exactly like
// the hierarchical mode. rank() below is a standalone best-first ordering
// over the regions. Crossing the
// boundary of region R costs |d(q, p_R) - r_R|, which by the triangle
// inequality is a lower bound on d(q, x) for every x on the other side;
// a leaf's score is the largest such bound on its path. The query's own
// PG value (score 0, no boundary crossed) always comes first.

namespace ceph::rados::vector_pivot_tree {

struct tree_t {
  uint32_t dim = 0;
  uint32_t levels = 0;
  // Region at depth l with prefix value v (bits b_1..b_l, b_1 at the LSB)
  // is node (2^l - 1) + v. pivots holds node_count() * dim floats.
  std::vector<float> pivots;
  std::vector<float> radii;

  size_t node_count() const { return radii.size(); }
  uint32_t leaf_count() const { return levels >= 32 ? 0 : (1u << levels); }
  const float *pivot(size_t node) const { return pivots.data() + node * dim; }
};

inline size_t node_index(uint32_t level, uint32_t prefix)
{
  return (size_t{1} << level) - 1 + prefix;
}

inline float l2_distance(const float *a, const float *b, uint32_t dim)
{
  double sum = 0.0;
  for (uint32_t i = 0; i < dim; ++i) {
    const double diff = static_cast<double>(a[i]) - b[i];
    sum += diff * diff;
  }
  return static_cast<float>(std::sqrt(sum));
}

inline uint64_t splitmix64(uint64_t x)
{
  x += 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}

struct build_params_t {
  uint32_t levels = 0;
  uint64_t seed = 0;
  // pivot candidates tried per region, and members used to score them
  uint32_t candidates = 8;
  uint32_t eval_samples = 256;
};

// samples: training vectors (dim floats each). Returns an empty tree
// (levels == 0) when there is nothing to train on.
inline tree_t build(const std::vector<const float *>& samples, uint32_t dim,
                    const build_params_t& params)
{
  tree_t tree;
  if (samples.empty() || dim == 0 || params.levels == 0 ||
      params.levels >= 32) {
    return tree;
  }
  tree.dim = dim;
  tree.levels = params.levels;
  const size_t nodes = (size_t{1} << params.levels) - 1;
  tree.pivots.assign(nodes * dim, 0.0f);
  tree.radii.assign(nodes, 0.0f);

  // members[prefix] for the current level
  std::vector<std::vector<uint32_t>> members(1);
  members[0].resize(samples.size());
  for (uint32_t i = 0; i < samples.size(); ++i) members[0][i] = i;

  std::vector<float> dist;
  for (uint32_t level = 0; level < params.levels; ++level) {
    std::vector<std::vector<uint32_t>> next(size_t{1} << (level + 1));
    for (uint32_t prefix = 0; prefix < members.size(); ++prefix) {
      const auto& set = members[prefix];
      const size_t node = node_index(level, prefix);
      float *pivot = tree.pivots.data() + node * dim;
      if (set.empty()) {
        // Untrained region: keep the parent's pivot and send everything
        // to bit 0 (r = +inf), so the empty sibling stays empty.
        if (level > 0) {
          const uint32_t parent_prefix = prefix & ((1u << (level - 1)) - 1);
          const float *parent = tree.pivot(node_index(level - 1, parent_prefix));
          std::memcpy(pivot, parent, sizeof(float) * dim);
        }
        tree.radii[node] = std::numeric_limits<float>::infinity();
        continue;
      }

      // pivot: the candidate with the largest distance variance
      const size_t n = set.size();
      const size_t eval_n = std::min<size_t>(n, std::max<uint32_t>(1, params.eval_samples));
      const size_t eval_stride = std::max<size_t>(1, n / eval_n);
      const uint32_t cand_n = static_cast<uint32_t>(
          std::min<size_t>(n, std::max<uint32_t>(1, params.candidates)));
      uint32_t best = set[0];
      double best_var = -1.0;
      for (uint32_t c = 0; c < cand_n; ++c) {
        const uint64_t h = splitmix64(
            params.seed ^ splitmix64((static_cast<uint64_t>(node) << 32) | c));
        const uint32_t cand = set[h % n];
        double sum = 0.0, sum_sq = 0.0;
        size_t cnt = 0;
        for (size_t i = 0; i < n && cnt < eval_n; i += eval_stride, ++cnt) {
          const double d = l2_distance(samples[cand], samples[set[i]], dim);
          sum += d;
          sum_sq += d * d;
        }
        const double mean = sum / cnt;
        const double var = sum_sq / cnt - mean * mean;
        if (var > best_var) {
          best_var = var;
          best = cand;
        }
      }
      std::memcpy(pivot, samples[best], sizeof(float) * dim);

      // r_R: median member distance to the pivot
      dist.resize(n);
      for (size_t i = 0; i < n; ++i) {
        dist[i] = l2_distance(pivot, samples[set[i]], dim);
      }
      std::vector<float> sorted(dist);
      std::nth_element(sorted.begin(), sorted.begin() + n / 2, sorted.end());
      const float radius = sorted[n / 2];
      tree.radii[node] = radius;

      for (size_t i = 0; i < n; ++i) {
        const uint32_t bit = dist[i] < radius ? 0 : 1;
        next[prefix | (bit << level)].push_back(set[i]);
      }
    }
    members.swap(next);
  }
  return tree;
}

// PGValue(v): one bit per level, b_1 at the LSB.
inline uint32_t pg_value(const tree_t& tree, const float *v)
{
  uint32_t prefix = 0;
  for (uint32_t level = 0; level < tree.levels; ++level) {
    const size_t node = node_index(level, prefix);
    const uint32_t bit =
        l2_distance(v, tree.pivot(node), tree.dim) < tree.radii[node] ? 0 : 1;
    prefix |= bit << level;
  }
  return prefix;
}

// Multi-probe descent, the training-derived counterpart of
// vector_pg_lsh_hierarchical::descend(): at each level l with bit l of
// flip_mask set, the descent takes the sibling of the side v falls on and
// keeps descending with v inside the sibling's subtree, so every bit below
// a flip is re-decided by that subtree's own pivots and boundaries.
// flip_mask 0 gives pg_value(). margins, if not null, receives one entry
// per level: |d(v, p_R) - r_R| for the region R visited at that level
// (+inf for an untrained region).
inline uint32_t descend(const tree_t& tree, const float *v, uint32_t flip_mask,
                        std::vector<float> *margins = nullptr)
{
  if (margins != nullptr) {
    margins->clear();
    margins->reserve(tree.levels);
  }
  uint32_t prefix = 0;
  for (uint32_t level = 0; level < tree.levels; ++level) {
    const size_t node = node_index(level, prefix);
    const float d = l2_distance(v, tree.pivot(node), tree.dim);
    const float r = tree.radii[node];
    const uint32_t bit = (d < r ? 0u : 1u) ^ ((flip_mask >> level) & 1u);
    if (margins != nullptr) {
      margins->push_back(std::isinf(r) ? std::numeric_limits<float>::infinity()
                                       : std::fabs(d - r));
    }
    prefix |= bit << level;
  }
  return prefix;
}

// Up to keep_n leaf values (PG values), best first; the first entry is
// always pg_value(tree, q). Ties: fewer boundary crossings, then the
// smaller value.
inline std::vector<uint32_t> rank(const tree_t& tree, const float *q,
                                  uint32_t keep_n)
{
  struct entry_t {
    float bound;
    uint32_t crossings;
    uint32_t level;
    uint32_t prefix;
  };
  auto worse = [](const entry_t& a, const entry_t& b) {
    if (a.bound != b.bound) return a.bound > b.bound;
    if (a.crossings != b.crossings) return a.crossings > b.crossings;
    if (a.level != b.level) return a.level < b.level;
    return a.prefix > b.prefix;
  };
  std::vector<uint32_t> out;
  if (tree.levels == 0 || keep_n == 0) return out;
  out.reserve(std::min<uint32_t>(keep_n, tree.leaf_count()));
  std::priority_queue<entry_t, std::vector<entry_t>, decltype(worse)> heap(worse);
  heap.push({0.0f, 0, 0, 0});
  while (!heap.empty() && out.size() < keep_n) {
    const entry_t e = heap.top();
    heap.pop();
    if (e.level == tree.levels) {
      out.push_back(e.prefix);
      continue;
    }
    const size_t node = node_index(e.level, e.prefix);
    const float d = l2_distance(q, tree.pivot(node), tree.dim);
    const float r = tree.radii[node];
    const uint32_t own = d < r ? 0 : 1;
    const float margin = std::isinf(r) ? std::numeric_limits<float>::infinity()
                                       : std::fabs(d - r);
    heap.push({e.bound, e.crossings, e.level + 1, e.prefix | (own << e.level)});
    heap.push({std::max(e.bound, margin), e.crossings + 1, e.level + 1,
               e.prefix | ((1u - own) << e.level)});
  }
  return out;
}

// FNV-1a over (dim, levels, pivots, radii), printed by the bench so a query
// run can be checked against the load run (the model is not persisted).
inline uint64_t digest(const tree_t& tree)
{
  uint64_t h = 1469598103934665603ULL;
  auto mix = [&h](const void *p, size_t len) {
    const auto *b = static_cast<const unsigned char *>(p);
    for (size_t i = 0; i < len; ++i) h = (h ^ b[i]) * 1099511628211ULL;
  };
  mix(&tree.dim, sizeof(tree.dim));
  mix(&tree.levels, sizeof(tree.levels));
  mix(tree.pivots.data(), tree.pivots.size() * sizeof(float));
  mix(tree.radii.data(), tree.radii.size() * sizeof(float));
  return h;
}

} // namespace ceph::rados::vector_pivot_tree

#endif // CEPH_COMMON_VECTOR_PIVOT_TREE_H
