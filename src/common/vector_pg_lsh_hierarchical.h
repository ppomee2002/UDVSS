// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_COMMON_VECTOR_PG_LSH_HIERARCHICAL_H
#define CEPH_COMMON_VECTOR_PG_LSH_HIERARCHICAL_H

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <span>
#include <vector>

// Hierarchical binary partitioning for the PG-LSH bucket id
// (placement_mode_hierarchical_lsh in librados/vector_pg_lsh.h).
//
// Unlike pg_lsh_v0_hyperplane_sign() (vector_pg_lsh_placement.h), whose
// hyperplane for bit i is fixed by (seed, table, i, dimension), the
// hyperplane at each level here depends on the region the vector has
// reached so far. For a region R (the bit prefix decided so far):
//
//   h_R      = PRNG(Hash(R)) / ||PRNG(Hash(R))||_2
//   b_R      = (v - p_R) . h_R >= 0 ? 1 : 0
//   p_root   = anchor
//   delta_l  = S / 2^(l+1)                 (l = |R|)
//   p_{R b}  = p_R + (2 b - 1) delta_l h_R
//
// and PGValue(v) = b_1 b_2 ... b_L. The level-l bit is stored at bit l of
// the returned value (b_1 is the LSB), so value & (2^n - 1) is the region
// at depth n: a PG split to 2^(n+1) adds bit n and keeps bits [0, n), the
// same LSB growth as the fixed-LSH aligned locators.
//
// A region is keyed by the integer pair (level, prefix), prefix holding
// b_1..b_level in its low bits. level is part of the key because prefix 0
// at level 1 is not the root.
//
// Everything is integer mixing plus IEEE +, *, sqrt and ldexp, all exactly
// rounded, so every client derives bit-identical hyperplanes and bits for
// the same (seed, table, vector, anchor, S).
namespace ceph::rados::vector_pg_lsh_hierarchical {

inline constexpr uint32_t max_levels = 32;

inline uint64_t splitmix64(uint64_t value)
{
  value += 0x9e3779b97f4a7c15ULL;
  value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
  value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
  return value ^ (value >> 31);
}

// Hash(R). Bits of prefix at or above `level` are ignored.
inline uint64_t region_hash(uint32_t seed,
                            uint32_t table,
                            uint32_t level,
                            uint32_t prefix)
{
  if (level < 32) {
    prefix &= (uint32_t{1} << level) - 1;
  }
  uint64_t h = splitmix64(0x48494552ULL ^ seed);  // "HIER"
  h = splitmix64(h ^ ((uint64_t{table} << 32) | level));
  return splitmix64(h ^ prefix);
}

// h_R: component i is PRNG output i of the region hash, mapped to
// [-1, 1), then the whole vector is L2-normalized.
inline void region_hyperplane(uint64_t region_hash,
                              uint32_t dimension,
                              std::vector<double> *hyperplane)
{
  hyperplane->resize(dimension);
  double norm_squared = 0;
  for (uint32_t dim = 0; dim < dimension; ++dim) {
    const uint64_t r = splitmix64(region_hash + uint64_t{dim});
    // 53 random bits -> [0, 1), exact in a double.
    const double unit = std::ldexp(static_cast<double>(r >> 11), -53);
    const double component = 2.0 * unit - 1.0;
    (*hyperplane)[dim] = component;
    norm_squared += component * component;
  }
  if (!(norm_squared > 0)) {
    // Only reachable for dimension 0 or an all-zero draw; keep h_R a
    // unit vector regardless.
    if (dimension != 0) {
      std::fill(hyperplane->begin(), hyperplane->end(), 0.0);
      (*hyperplane)[0] = 1.0;
    }
    return;
  }
  const double inverse_norm = 1.0 / std::sqrt(norm_squared);
  for (double& component : *hyperplane) {
    component *= inverse_norm;
  }
}

// delta_l = S / 2^(l+1).
inline double level_offset(double scale, uint32_t level)
{
  return std::ldexp(scale, -static_cast<int>(level + 1));
}

// p_{R b} = p_R + (2 b - 1) delta_l h_R, in place.
inline void step_pivot(std::vector<double> *pivot,
                       const std::vector<double>& hyperplane,
                       uint32_t level,
                       uint32_t bit,
                       double scale)
{
  const double signed_offset =
    (bit != 0 ? 1.0 : -1.0) * level_offset(scale, level);
  for (size_t dim = 0; dim < pivot->size(); ++dim) {
    (*pivot)[dim] += signed_offset * hyperplane[dim];
  }
}

// One recursive partition step, as recorded by descend().
struct level_trace_t {
  uint32_t level = 0;
  uint32_t prefix = 0;      // region R: b_1..b_level
  double projection = 0;    // (v - p_R) . h_R
  uint32_t bit = 0;         // bit taken (after any flip)
};

struct params_t {
  uint32_t seed = 0;
  uint32_t table = 0;
  uint32_t levels = 0;
  std::span<const double> anchor;
  double scale = 0;
};

// The single put/query entry point. Descends `levels` regions from the
// anchor and returns b_1..b_L (b_1 at bit 0) in *value.
//
// flip_mask is for multi-probe: at each level l with bit l set, the
// descent takes the sibling of the side v falls on and then keeps
// descending with v from the sibling's pivot, so every bit below a flip
// is re-decided by the sibling subtree's own hyperplanes. flip_mask 0 is
// v's own region and is what put uses. Each flip_mask gives a distinct
// value (the flip at level l is recoverable as b_l ^ natural side), so
// enumerating masks never yields a region twice.
//
// trace, if not null, receives exactly `levels` entries, one per
// recursive partition.
inline int descend(std::span<const float> values,
                   const params_t& params,
                   uint32_t flip_mask,
                   uint32_t *value,
                   std::vector<level_trace_t> *trace = nullptr)
{
  if (value == nullptr || params.levels == 0 ||
      params.levels > max_levels ||
      params.anchor.size() != values.size() || values.empty() ||
      !(params.scale > 0) || !std::isfinite(params.scale)) {
    return -EINVAL;
  }
  const uint32_t dimension = static_cast<uint32_t>(values.size());
  std::vector<double> pivot(params.anchor.begin(), params.anchor.end());
  std::vector<double> hyperplane;
  if (trace != nullptr) {
    trace->clear();
    trace->reserve(params.levels);
  }

  uint32_t prefix = 0;
  for (uint32_t level = 0; level < params.levels; ++level) {
    region_hyperplane(
        region_hash(params.seed, params.table, level, prefix),
        dimension, &hyperplane);
    double projection = 0;
    for (uint32_t dim = 0; dim < dimension; ++dim) {
      projection +=
        (static_cast<double>(values[dim]) - pivot[dim]) * hyperplane[dim];
    }
    const uint32_t bit =
      (projection >= 0 ? 1U : 0U) ^ ((flip_mask >> level) & 1U);
    if (trace != nullptr) {
      trace->push_back({level, prefix, projection, bit});
    }
    prefix |= bit << level;
    step_pivot(&pivot, hyperplane, level, bit, params.scale);
  }
  *value = prefix;
  return 0;
}

} // namespace ceph::rados::vector_pg_lsh_hierarchical

#endif
