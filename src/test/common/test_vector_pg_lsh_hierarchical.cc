// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#include "common/vector_pg_lsh_hierarchical.h"
#include "librados/vector_placement.h"
#include "librados/vector_pg_lsh.h"
#include "include/rados.h"

#include <cmath>
#include <set>
#include <vector>

#include <gtest/gtest.h>

namespace vp = librados::vector_placement;
namespace pg_lsh = librados::vector_pg_lsh;
namespace hier = ceph::rados::vector_pg_lsh_hierarchical;

namespace {

constexpr uint32_t kDim = 16;
constexpr uint32_t kSeed = 1315423911u;
constexpr double kScale = 8.0;

// Integer-derived components, so the inputs themselves need no libm.
std::vector<float> make_values(int variant)
{
  std::vector<float> values(kDim);
  for (int i = 0; i < static_cast<int>(kDim); ++i) {
    values[i] =
      static_cast<float>(((i * 7 + variant * 13) % 23) - 11) * 0.25f;
  }
  return values;
}

ceph::bufferlist to_bl(const std::vector<float>& values)
{
  ceph::bufferlist bl;
  bl.append(reinterpret_cast<const char*>(values.data()),
            values.size() * sizeof(float));
  return bl;
}

std::vector<double> make_anchor()
{
  std::vector<double> anchor;
  EXPECT_EQ(0, vp::pg_lsh_v0_random_anchor(kDim, kSeed, &anchor));
  return anchor;
}

hier::params_t params_for(const std::vector<double>& anchor,
                          uint32_t levels, uint32_t table = 0)
{
  return {kSeed, table, levels, std::span<const double>(anchor), kScale};
}

uint32_t descend_value(const std::vector<float>& values,
                       const std::vector<double>& anchor,
                       uint32_t levels, uint32_t flip_mask = 0)
{
  uint32_t value = 0;
  EXPECT_EQ(0, hier::descend(values, params_for(anchor, levels), flip_mask,
                             &value));
  return value;
}

double dot(const std::vector<double>& a, const std::vector<double>& b)
{
  double sum = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    sum += a[i] * b[i];
  }
  return sum;
}

// A valid non-locator index config (creation_pg_num == pool pg_num).
pg_lsh::index_config_t make_config(uint32_t placement_mode, uint32_t k)
{
  pg_lsh::index_config_t config;
  config.dimension = kDim;
  config.data_type = ceph::rados::vector_data_type_float32;
  config.distance_metric = ceph::rados::vector_distance_metric_euclidean;
  config.k = k;
  config.l = 4;
  config.seed = kSeed;
  config.d = 1;
  config.creation_pg_num = 1024;
  config.creation_pgp_num = 1024;
  config.anchor_mode = pg_lsh::anchor_mode_random;
  config.anchor = make_anchor();
  config.placement_mode = placement_mode;
  if (placement_mode == pg_lsh::placement_mode_hierarchical_lsh) {
    config.hierarchical_scale = kScale;
  }
  return config;
}

const pg_lsh::pool_pg_info_t kPool = {1024, 1024, 1};

} // namespace

// Same (seed, vector, config) -> same PG value, every time and on every
// path that computes it.
TEST(VectorPgLshHierarchical, DeterministicValue)
{
  const auto anchor = make_anchor();
  for (int variant = 0; variant < 8; ++variant) {
    const auto values = make_values(variant);
    const uint32_t first = descend_value(values, anchor, 12);
    for (int repeat = 0; repeat < 4; ++repeat) {
      EXPECT_EQ(first, descend_value(values, anchor, 12));
    }
    // A fresh copy of the anchor must not matter either.
    const std::vector<double> anchor_copy = anchor;
    EXPECT_EQ(first, descend_value(values, anchor_copy, 12));
  }
}

// Put and query go through the same helper: the write PG and the query's
// first (exact) PG coincide, and the exact groups match descend().
TEST(VectorPgLshHierarchical, PutAndQueryAgree)
{
  const auto config =
    make_config(pg_lsh::placement_mode_hierarchical_lsh, 10);
  ASSERT_EQ(0, pg_lsh::validate_index_config(config, kPool));
  for (int variant = 0; variant < 16; ++variant) {
    const auto bl = to_bl(make_values(variant));
    std::vector<uint32_t> write_pgs;
    ASSERT_EQ(0, pg_lsh::select_write_pgs(bl, config, kPool, &write_pgs));
    ASSERT_EQ(1u, write_pgs.size());

    pg_lsh::query_params_t params;
    params.m = 1;
    std::vector<vp::pg_lsh_v0_ranked_pg_t> query_pgs;
    ASSERT_EQ(0, pg_lsh::select_query_pgs(
        bl, config, params, kPool, &query_pgs, nullptr));
    ASSERT_EQ(1u, query_pgs.size());
    EXPECT_EQ(write_pgs[0], query_pgs[0].pg);

    std::vector<vp::pg_lsh_v0_group_t> exact;
    ASSERT_EQ(0, pg_lsh::exact_lsh_groups(bl, config, &exact));
    ASSERT_EQ(config.l, exact.size());
    for (const auto& group : exact) {
      uint32_t value = 0;
      ASSERT_EQ(0, hier::descend(
          make_values(variant),
          {config.seed, group.table, config.k,
           std::span<const double>(config.anchor), config.hierarchical_scale},
          0, &value));
      EXPECT_EQ(value, group.lsh_bucket_id);
    }
  }
}

// h_R depends on R: sibling regions, different depths and the root all
// get different hyperplanes, and a vector's level-1 hyperplane is the one
// of the region its first bit chose.
TEST(VectorPgLshHierarchical, PrefixChangesHyperplane)
{
  std::vector<double> root, left, right, left_level2;
  hier::region_hyperplane(hier::region_hash(kSeed, 0, 0, 0), kDim, &root);
  hier::region_hyperplane(hier::region_hash(kSeed, 0, 1, 0), kDim, &left);
  hier::region_hyperplane(hier::region_hash(kSeed, 0, 1, 1), kDim, &right);
  hier::region_hyperplane(hier::region_hash(kSeed, 0, 2, 0), kDim,
                          &left_level2);
  EXPECT_NE(root, left);
  EXPECT_NE(left, right);
  EXPECT_NE(left, left_level2);  // prefix 0 at level 1 vs level 2
  for (const auto *h : {&root, &left, &right, &left_level2}) {
    EXPECT_NEAR(1.0, dot(*h, *h), 1e-12);
  }
  // Bits above `level` are not part of R.
  EXPECT_EQ(hier::region_hash(kSeed, 0, 1, 1),
            hier::region_hash(kSeed, 0, 1, 0xffffffffu));

  // Different seed or table -> different hyperplane for the same R.
  EXPECT_NE(hier::region_hash(kSeed, 0, 1, 1),
            hier::region_hash(kSeed + 1, 0, 1, 1));
  EXPECT_NE(hier::region_hash(kSeed, 0, 1, 1),
            hier::region_hash(kSeed, 1, 1, 1));

  // Two vectors whose first bits differ see different level-1 regions.
  const auto anchor = make_anchor();
  std::vector<hier::level_trace_t> trace_a, trace_b;
  int found = -1;
  uint32_t value_a = 0, value_b = 0;
  ASSERT_EQ(0, hier::descend(make_values(0), params_for(anchor, 4), 0,
                             &value_a, &trace_a));
  for (int variant = 1; variant < 64 && found < 0; ++variant) {
    ASSERT_EQ(0, hier::descend(make_values(variant), params_for(anchor, 4),
                               0, &value_b, &trace_b));
    if ((value_a & 1u) != (value_b & 1u)) {
      found = variant;
    }
  }
  ASSERT_GE(found, 0);
  EXPECT_NE(trace_a[1].prefix, trace_b[1].prefix);
  std::vector<double> h_a, h_b;
  hier::region_hyperplane(
      hier::region_hash(kSeed, 0, 1, trace_a[1].prefix), kDim, &h_a);
  hier::region_hyperplane(
      hier::region_hash(kSeed, 0, 1, trace_b[1].prefix), kDim, &h_b);
  EXPECT_NE(h_a, h_b);
}

// p_{R0} = p_R - delta_l h_R and p_{R1} = p_R + delta_l h_R, with
// delta_l = S / 2^(l+1).
TEST(VectorPgLshHierarchical, ChildPivotMovesAlongHyperplane)
{
  const auto parent = make_anchor();
  std::vector<double> h;
  hier::region_hyperplane(hier::region_hash(kSeed, 0, 3, 5), kDim, &h);
  for (uint32_t level = 0; level < 6; ++level) {
    const double delta = kScale / std::pow(2.0, level + 1);
    EXPECT_EQ(delta, hier::level_offset(kScale, level));
    for (uint32_t bit = 0; bit < 2; ++bit) {
      std::vector<double> child = parent;
      hier::step_pivot(&child, h, level, bit, kScale);
      std::vector<double> moved(kDim);
      for (uint32_t i = 0; i < kDim; ++i) {
        moved[i] = child[i] - parent[i];
      }
      const double sign = bit ? 1.0 : -1.0;
      // Along h_R, with length delta_l and the bit's sign...
      EXPECT_NEAR(sign * delta, dot(moved, h), 1e-12);
      EXPECT_NEAR(delta * delta, dot(moved, moved), 1e-12);
      // ...component-wise, nothing else moved.
      for (uint32_t i = 0; i < kDim; ++i) {
        EXPECT_NEAR(sign * delta * h[i], moved[i], 1e-12);
      }
    }
  }
}

// L bits come from exactly L recursive partitions; replaying the
// recurrence by hand from the anchor reproduces each step.
TEST(VectorPgLshHierarchical, LBitsFromLPartitions)
{
  const auto anchor = make_anchor();
  for (uint32_t levels = 1; levels <= 16; ++levels) {
    for (int variant = 0; variant < 4; ++variant) {
      const auto values = make_values(variant);
      uint32_t value = 0;
      std::vector<hier::level_trace_t> trace;
      ASSERT_EQ(0, hier::descend(values, params_for(anchor, levels), 0,
                                 &value, &trace));
      ASSERT_EQ(levels, trace.size());
      if (levels < 32) {
        EXPECT_LT(value, 1u << levels);
      }

      std::vector<double> pivot = anchor;
      uint32_t prefix = 0;
      for (uint32_t l = 0; l < levels; ++l) {
        EXPECT_EQ(l, trace[l].level);
        EXPECT_EQ(prefix, trace[l].prefix);
        std::vector<double> h;
        hier::region_hyperplane(hier::region_hash(kSeed, 0, l, prefix),
                                kDim, &h);
        double projection = 0;
        for (uint32_t i = 0; i < kDim; ++i) {
          projection += (static_cast<double>(values[i]) - pivot[i]) * h[i];
        }
        EXPECT_EQ(projection, trace[l].projection);
        const uint32_t bit = projection >= 0 ? 1u : 0u;
        EXPECT_EQ(bit, trace[l].bit);
        EXPECT_EQ(bit, (value >> l) & 1u);
        prefix |= bit << l;
        hier::step_pivot(&pivot, h, l, bit, kScale);
      }
      EXPECT_EQ(prefix, value);
    }
  }
  uint32_t value = 0;
  EXPECT_EQ(-EINVAL, hier::descend(make_values(0), params_for(anchor, 0), 0,
                                   &value));
}

// The partition is not degenerate at this S: varied inputs spread over
// many regions and both sides of each top-level split.
TEST(VectorPgLshHierarchical, ValuesSpread)
{
  const auto anchor = make_anchor();
  std::set<uint32_t> values;
  uint32_t ones = 0;
  for (int variant = 0; variant < 23; ++variant) {
    const uint32_t value = descend_value(make_values(variant), anchor, 8);
    values.insert(value);
    ones += value & 1u;
  }
  EXPECT_GE(values.size(), 8u);
  EXPECT_GT(ones, 0u);
  EXPECT_LT(ones, 23u);
}

// Growing PGs 2^L -> 2^(L+1) uses one more level: the low L bits are
// unchanged, so each old PG splits into exactly two children (old, and
// old | 2^L), and merging back recovers the old PG.
TEST(VectorPgLshHierarchical, SplitAddsOneBitKeepingLowBits)
{
  const auto anchor = make_anchor();
  constexpr uint32_t kMaxBits = 12;
  for (uint32_t L = 1; L < kMaxBits; ++L) {
    const uint32_t pg_num = 1u << L;
    std::set<std::pair<uint32_t, uint32_t>> parent_child;
    for (int variant = 0; variant < 64; ++variant) {
      const auto values = make_values(variant);
      // Bucket ids at the full k, masked to pg_num, as the aligned
      // locators place them.
      const uint32_t full = descend_value(values, anchor, kMaxBits);
      const uint32_t pg_before = full & (pg_num - 1);
      const uint32_t pg_after = full & (2 * pg_num - 1);
      EXPECT_EQ(pg_before, pg_after & (pg_num - 1));
      EXPECT_TRUE(pg_after == pg_before || pg_after == (pg_before | pg_num));
      // Truncating the descent gives the same region as masking.
      EXPECT_EQ(pg_before, descend_value(values, anchor, L));
      EXPECT_EQ(pg_after, descend_value(values, anchor, L + 1));
      parent_child.insert({pg_before, pg_after});
    }
    for (const auto& [parent, child] : parent_child) {
      EXPECT_EQ(parent, child & (pg_num - 1));
    }
  }
}

// Multi-probe flips a level and re-descends the sibling subtree: bits
// above the flip match the exact bucket, the flipped bit is inverted, and
// every lower bit is decided by the sibling's own hyperplanes.
TEST(VectorPgLshHierarchical, MultiProbeReDescendsSibling)
{
  constexpr uint32_t kBits = 8;
  const auto config =
    make_config(pg_lsh::placement_mode_hierarchical_lsh, kBits);
  const auto values = make_values(5);
  const auto bl = to_bl(values);
  const uint32_t exact = descend_value(values, config.anchor, kBits);

  // Re-descent is not a plain XOR: for some level the sibling subtree
  // decides at least one lower bit differently.
  bool any_rerouted = false;
  for (uint32_t j = 0; j + 1 < kBits; ++j) {
    if (descend_value(values, config.anchor, kBits, 1u << j) !=
        (exact ^ (1u << j))) {
      any_rerouted = true;
    }
  }
  EXPECT_TRUE(any_rerouted);

  for (uint32_t j = 0; j < kBits; ++j) {
    const uint32_t probe = descend_value(values, config.anchor, kBits,
                                         1u << j);
    const uint32_t above = (1u << j) - 1;
    EXPECT_EQ(exact & above, probe & above);
    EXPECT_NE(exact & (1u << j), probe & (1u << j));

    // Re-descent by hand: walk the exact path to level j, take the
    // sibling, then let v decide every remaining level.
    std::vector<double> pivot = config.anchor;
    uint32_t prefix = 0;
    for (uint32_t l = 0; l < kBits; ++l) {
      std::vector<double> h;
      hier::region_hyperplane(hier::region_hash(kSeed, 0, l, prefix),
                              kDim, &h);
      double projection = 0;
      for (uint32_t i = 0; i < kDim; ++i) {
        projection += (static_cast<double>(values[i]) - pivot[i]) * h[i];
      }
      uint32_t bit = projection >= 0 ? 1u : 0u;
      if (l == j) {
        bit ^= 1u;
      }
      prefix |= bit << l;
      hier::step_pivot(&pivot, h, l, bit, kScale);
    }
    EXPECT_EQ(prefix, probe);
  }

  // The routed query list: exact first, C(k, r) distinct probes per
  // radius, no region twice, and margin ordering only reorders.
  for (bool margin : {false, true}) {
    vp::pg_lsh_v0_query_routing_t routing;
    routing.table_count = 1;
    routing.margin_ordered_probes = margin;
    std::vector<vp::pg_lsh_v0_group_t> groups;
    ASSERT_EQ(0, pg_lsh::query_lsh_groups(bl, config, 2, routing, &groups));
    ASSERT_EQ(1u + kBits + kBits * (kBits - 1) / 2, groups.size());
    EXPECT_EQ(exact, groups[0].lsh_bucket_id);
    EXPECT_EQ(0u, groups[0].hamming_distance);
    std::set<uint32_t> ids;
    for (const auto& group : groups) {
      EXPECT_EQ(0u, group.table);
      EXPECT_TRUE(ids.insert(group.lsh_bucket_id).second);
      uint32_t mask_value = 0;
      // Each group is the descent of some mask of its distance.
      bool matched = false;
      for (const uint32_t mask : vp::pg_lsh_v0_hamming_masks_at_distance(
               kBits, group.hamming_distance)) {
        mask_value = descend_value(values, config.anchor, kBits, mask);
        if (mask_value == group.lsh_bucket_id) {
          matched = true;
          break;
        }
      }
      EXPECT_TRUE(matched);
    }
  }
}

// fixed_lsh is the default and is bit-for-bit what it was. The golden
// values were taken from pg_lsh_v0_* before placement_mode existed.
TEST(VectorPgLshHierarchical, FixedLshUnchanged)
{
  pg_lsh::index_config_t defaults;
  EXPECT_EQ(pg_lsh::placement_mode_fixed_lsh, defaults.placement_mode);
  EXPECT_EQ(0.0, defaults.hierarchical_scale);

  const uint32_t golden[6][4] = {
    {0x4f4u, 0x24cu, 0x1e0u, 0x897u},
    {0xa6bu, 0xbaau, 0xf19u, 0x46au},
    {0xc98u, 0xe90u, 0x07cu, 0x9b7u},
    {0xa6bu, 0x97fu, 0xa08u, 0x6ebu},
    {0x59au, 0xd02u, 0xf7eu, 0x9b0u},
    {0x77du, 0xad5u, 0x280u, 0xe4du},
  };
  const auto config = make_config(pg_lsh::placement_mode_fixed_lsh, 12);
  ASSERT_EQ(0, pg_lsh::validate_index_config(config, kPool));
  for (int variant = 0; variant < 6; ++variant) {
    const auto bl = to_bl(make_values(variant));
    std::vector<vp::pg_lsh_v0_group_t> legacy, dispatched;
    ASSERT_EQ(0, vp::pg_lsh_v0_exact_groups(bl, kDim, 12, 4, kSeed, &legacy));
    ASSERT_EQ(0, pg_lsh::exact_lsh_groups(bl, config, &dispatched));
    ASSERT_EQ(4u, legacy.size());
    ASSERT_EQ(4u, dispatched.size());
    for (uint32_t t = 0; t < 4; ++t) {
      EXPECT_EQ(golden[variant][t], legacy[t].lsh_bucket_id);
      EXPECT_EQ(golden[variant][t], dispatched[t].lsh_bucket_id);
    }
  }

  const uint64_t golden_fnv[2] = {0xe6fdac1c6a98ed19ull,
                                  0xc0a3d13dcd64e45full};
  for (int margin = 0; margin < 2; ++margin) {
    vp::pg_lsh_v0_query_routing_t routing;
    routing.table_count = 2;
    routing.margin_ordered_probes = margin;
    std::vector<vp::pg_lsh_v0_group_t> groups;
    ASSERT_EQ(0, pg_lsh::query_lsh_groups(
        to_bl(make_values(3)), config, 2, routing, &groups));
    EXPECT_EQ(158u, groups.size());
    uint64_t h = 1469598103934665603ull;
    for (const auto& g : groups) {
      h ^= (uint64_t(g.table) << 40) ^
        (uint64_t(g.hamming_distance) << 32) ^ g.lsh_bucket_id;
      h *= 1099511628211ull;
    }
    EXPECT_EQ(golden_fnv[margin], h);
  }
}

// A fixed_lsh config still encodes as v3 (compat 1); a hierarchical one
// as v4 with compat 4, and both round-trip.
TEST(VectorPgLshHierarchical, ConfigEncoding)
{
  const auto fixed = make_config(pg_lsh::placement_mode_fixed_lsh, 10);
  ceph::bufferlist fixed_bl;
  fixed.encode(fixed_bl);
  EXPECT_EQ(3, static_cast<uint8_t>(fixed_bl[0]));
  EXPECT_EQ(1, static_cast<uint8_t>(fixed_bl[1]));

  const auto hierarchical =
    make_config(pg_lsh::placement_mode_hierarchical_lsh, 10);
  ceph::bufferlist hier_bl;
  hierarchical.encode(hier_bl);
  EXPECT_EQ(4, static_cast<uint8_t>(hier_bl[0]));
  EXPECT_EQ(4, static_cast<uint8_t>(hier_bl[1]));
  // Same prefix: the v4 fields are only appended.
  EXPECT_EQ(fixed_bl.length() + sizeof(uint32_t) + sizeof(double),
            hier_bl.length());

  for (const auto *bl : {&fixed_bl, &hier_bl}) {
    pg_lsh::index_config_t decoded;
    auto p = bl->cbegin();
    decoded.decode(p);
    EXPECT_TRUE(p.end());
    const auto& expected = bl == &fixed_bl ? fixed : hierarchical;
    std::string mismatch;
    EXPECT_EQ(0, pg_lsh::compare_index_configs(expected, decoded, &mismatch))
      << mismatch;
  }
}

TEST(VectorPgLshHierarchical, ValidateScale)
{
  std::string field;
  auto config = make_config(pg_lsh::placement_mode_hierarchical_lsh, 10);
  ASSERT_EQ(0, pg_lsh::validate_index_config(config, kPool, &field));
  for (const double bad : {0.0, -1.0, std::nan(""), HUGE_VAL}) {
    config.hierarchical_scale = bad;
    EXPECT_EQ(-EINVAL, pg_lsh::validate_index_config(config, kPool, &field));
    EXPECT_EQ("hierarchical_scale", field);
  }

  auto fixed = make_config(pg_lsh::placement_mode_fixed_lsh, 10);
  fixed.hierarchical_scale = kScale;
  EXPECT_EQ(-EINVAL, pg_lsh::validate_index_config(fixed, kPool, &field));
  EXPECT_EQ("hierarchical_scale", field);

  fixed.hierarchical_scale = 0;
  fixed.placement_mode = 7;
  EXPECT_EQ(-EINVAL, pg_lsh::validate_index_config(fixed, kPool, &field));
  EXPECT_EQ("placement_mode", field);
}
