// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#include "common/vector_pivot_tree.h"
#include "librados/vector_placement.h"
#include "librados/vector_pg_lsh.h"
#include "include/rados.h"

#include <algorithm>
#include <cmath>
#include <random>
#include <set>
#include <vector>

#include <gtest/gtest.h>

namespace vpt = ceph::rados::vector_pivot_tree;

namespace {

std::vector<std::vector<float>> make_data(size_t n, uint32_t dim, uint32_t seed)
{
  std::mt19937 gen(seed);
  std::normal_distribution<float> normal(0.0f, 1.0f);
  std::vector<std::vector<float>> rows(n, std::vector<float>(dim));
  for (auto& row : rows) {
    for (auto& x : row) x = normal(gen);
  }
  return rows;
}

std::vector<const float *> ptrs(const std::vector<std::vector<float>>& rows)
{
  std::vector<const float *> out;
  for (const auto& r : rows) out.push_back(r.data());
  return out;
}

vpt::tree_t train(const std::vector<std::vector<float>>& rows, uint32_t dim,
                  uint32_t levels, uint64_t seed = 7)
{
  vpt::build_params_t params;
  params.levels = levels;
  params.seed = seed;
  return vpt::build(ptrs(rows), dim, params);
}

// Lower bound on d(q, x) for every x whose PG value is `leaf`: the largest
// |d(q, p_R) - r_R| over the regions on leaf's path where q's bit differs.
float path_bound(const vpt::tree_t& tree, const float *q, uint32_t leaf)
{
  float bound = 0.0f;
  uint32_t prefix = 0;
  for (uint32_t level = 0; level < tree.levels; ++level) {
    const size_t node = vpt::node_index(level, prefix);
    const float d = vpt::l2_distance(q, tree.pivot(node), tree.dim);
    const float r = tree.radii[node];
    const uint32_t own = d < r ? 0 : 1;
    const uint32_t bit = (leaf >> level) & 1u;
    if (bit != own) bound = std::max(bound, std::fabs(d - r));
    prefix |= bit << level;
  }
  return bound;
}

} // namespace

TEST(VectorPivotTree, PgValueFollowsPivotAndBoundaryPerLevel)
{
  const uint32_t dim = 8;
  const auto rows = make_data(2000, dim, 1);
  const auto tree = train(rows, dim, 6);
  ASSERT_EQ(6u, tree.levels);
  ASSERT_EQ(63u, tree.node_count());
  for (size_t i = 0; i < rows.size(); i += 37) {
    const float *v = rows[i].data();
    uint32_t prefix = 0;
    for (uint32_t level = 0; level < tree.levels; ++level) {
      const size_t node = vpt::node_index(level, prefix);
      const float d = vpt::l2_distance(v, tree.pivot(node), dim);
      const uint32_t bit = d < tree.radii[node] ? 0u : 1u;
      prefix |= bit << level;  // b_1 at the LSB
    }
    EXPECT_EQ(prefix, vpt::pg_value(tree, v));
    EXPECT_LT(vpt::pg_value(tree, v), tree.leaf_count());
  }
}

TEST(VectorPivotTree, LowBitsAreTheShallowRegion)
{
  const uint32_t dim = 8;
  const auto rows = make_data(4000, dim, 2);
  const auto deep = train(rows, dim, 7);
  const auto shallow = train(rows, dim, 4);
  // Same samples and seed: the first 4 levels are trained identically.
  for (size_t i = 0; i < rows.size(); i += 11) {
    EXPECT_EQ(vpt::pg_value(shallow, rows[i].data()),
              vpt::pg_value(deep, rows[i].data()) & 0xfu);
  }
}

TEST(VectorPivotTree, MedianBoundaryHalvesEveryRegion)
{
  const uint32_t dim = 16;
  const uint32_t levels = 5;
  const auto rows = make_data(1u << 12, dim, 3);
  const auto tree = train(rows, dim, levels);
  std::vector<size_t> fill(tree.leaf_count(), 0);
  for (const auto& r : rows) ++fill[vpt::pg_value(tree, r.data())];
  const size_t ideal = rows.size() / tree.leaf_count();
  for (const size_t f : fill) {
    EXPECT_GE(f + levels, ideal);
    EXPECT_LE(f, ideal + levels);
  }
}

TEST(VectorPivotTree, RankStartsAtOwnValueAndCoversAllLeaves)
{
  const uint32_t dim = 8;
  const auto rows = make_data(3000, dim, 4);
  const auto tree = train(rows, dim, 6);
  const auto queries = make_data(20, dim, 5);
  for (const auto& q : queries) {
    const auto all = vpt::rank(tree, q.data(), tree.leaf_count());
    ASSERT_EQ(tree.leaf_count(), all.size());
    EXPECT_EQ(vpt::pg_value(tree, q.data()), all.front());
    EXPECT_EQ(tree.leaf_count(), std::set<uint32_t>(all.begin(), all.end()).size());
    // Scores are non-decreasing in rank order.
    for (size_t i = 1; i < all.size(); ++i) {
      EXPECT_LE(path_bound(tree, q.data(), all[i - 1]),
                path_bound(tree, q.data(), all[i]));
    }
    // A shorter budget is a prefix of the full ranking.
    const auto top = vpt::rank(tree, q.data(), 7);
    ASSERT_EQ(7u, top.size());
    EXPECT_TRUE(std::equal(top.begin(), top.end(), all.begin()));
  }
}

TEST(VectorPivotTree, BoundNeverExceedsTrueDistance)
{
  const uint32_t dim = 8;
  const auto rows = make_data(3000, dim, 6);
  const auto tree = train(rows, dim, 6);
  const auto queries = make_data(10, dim, 8);
  for (const auto& q : queries) {
    for (size_t i = 0; i < rows.size(); i += 3) {
      const uint32_t leaf = vpt::pg_value(tree, rows[i].data());
      EXPECT_LE(path_bound(tree, q.data(), leaf),
                vpt::l2_distance(q.data(), rows[i].data(), dim) + 1e-4f);
    }
  }
}

TEST(VectorPivotTree, DeterministicInSamplesAndSeed)
{
  const uint32_t dim = 8;
  const auto rows = make_data(2000, dim, 9);
  EXPECT_EQ(vpt::digest(train(rows, dim, 5, 11)), vpt::digest(train(rows, dim, 5, 11)));
  EXPECT_NE(vpt::digest(train(rows, dim, 5, 11)), vpt::digest(train(rows, dim, 5, 12)));
}

TEST(VectorPivotTree, DegenerateRegionsStayUsable)
{
  const uint32_t dim = 4;
  // All samples identical: every region after the first split is empty on
  // one side; routing must still return valid, distinct PG values.
  std::vector<std::vector<float>> rows(64, std::vector<float>(dim, 1.0f));
  const auto tree = train(rows, dim, 4);
  ASSERT_EQ(4u, tree.levels);
  const uint32_t v = vpt::pg_value(tree, rows[0].data());
  EXPECT_LT(v, tree.leaf_count());
  const auto all = vpt::rank(tree, rows[0].data(), tree.leaf_count());
  ASSERT_EQ(tree.leaf_count(), all.size());
  EXPECT_EQ(v, all.front());
  EXPECT_EQ(tree.leaf_count(), std::set<uint32_t>(all.begin(), all.end()).size());

  EXPECT_EQ(0u, vpt::build({}, dim, {4, 1}).levels);
  EXPECT_TRUE(vpt::rank(vpt::tree_t{}, rows[0].data(), 4).empty());
}

// ---- placement_mode_training_derived through the pg-suboid plumbing ----

namespace {

namespace vp = librados::vector_placement;
namespace pg_lsh = librados::vector_pg_lsh;

constexpr uint32_t kDim = 16;
constexpr uint32_t kK = 10;
constexpr uint32_t kL = 4;
const pg_lsh::pool_pg_info_t kPool = {1024, 1024, 1};

ceph::bufferlist to_bl(const std::vector<float>& values)
{
  ceph::bufferlist bl;
  bl.append(reinterpret_cast<const char*>(values.data()),
            values.size() * sizeof(float));
  return bl;
}

pg_lsh::index_config_t td_config(const std::vector<std::vector<float>>& rows)
{
  pg_lsh::index_config_t config;
  config.dimension = kDim;
  config.data_type = ceph::rados::vector_data_type_float32;
  config.distance_metric = ceph::rados::vector_distance_metric_euclidean;
  config.k = kK;
  config.l = kL;
  config.seed = 1315423911u;
  config.d = 1;
  config.creation_pg_num = 1024;
  config.creation_pgp_num = 1024;
  config.anchor_mode = pg_lsh::anchor_mode_random;
  EXPECT_EQ(0, vp::pg_lsh_v0_random_anchor(kDim, config.seed, &config.anchor));
  config.placement_mode = pg_lsh::placement_mode_training_derived;
  auto trees = std::make_shared<std::vector<vpt::tree_t>>();
  for (uint32_t table = 0; table < kL; ++table) {
    trees->push_back(train(rows, kDim, kK, table + 1));
  }
  config.trained_trees = std::move(trees);
  return config;
}

} // namespace

TEST(VectorPivotTreeDescend, MaskZeroIsPgValueAndMasksAreDistinct)
{
  const auto rows = make_data(4000, 8, 21);
  const auto tree = train(rows, 8, 6);
  const auto queries = make_data(10, 8, 22);
  for (const auto& q : queries) {
    EXPECT_EQ(vpt::pg_value(tree, q.data()), vpt::descend(tree, q.data(), 0));
    std::set<uint32_t> values;
    for (uint32_t mask = 0; mask < tree.leaf_count(); ++mask) {
      const uint32_t v = vpt::descend(tree, q.data(), mask);
      // A flip at level 0 takes the other side of the root boundary.
      if (mask & 1u) {
        EXPECT_NE(vpt::pg_value(tree, q.data()) & 1u, v & 1u);
      }
      values.insert(v);
    }
    EXPECT_EQ(tree.leaf_count(), values.size());
  }
}

TEST(VectorPgLshTrainingDerived, PutAndQueryAgree)
{
  const auto rows = make_data(8000, kDim, 31);
  const auto config = td_config(rows);
  ASSERT_EQ(0, pg_lsh::validate_index_config(config, kPool));
  for (size_t i = 0; i < rows.size(); i += 401) {
    const auto bl = to_bl(rows[i]);
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
    ASSERT_EQ(kL, exact.size());
    for (const auto& group : exact) {
      EXPECT_EQ(vpt::pg_value((*config.trained_trees)[group.table], rows[i].data()),
                group.lsh_bucket_id);
    }
  }
}

TEST(VectorPgLshTrainingDerived, MarginOrderedProbes)
{
  const auto rows = make_data(8000, kDim, 41);
  const auto config = td_config(rows);
  const auto q = make_data(1, kDim, 42)[0];
  vp::pg_lsh_v0_query_routing_t routing;
  routing.margin_ordered_probes = true;
  routing.table_count = 1;
  std::vector<vp::pg_lsh_v0_group_t> groups;
  ASSERT_EQ(0, pg_lsh::query_lsh_groups(to_bl(q), config, 1, routing, &groups));
  ASSERT_EQ(1u + kK, groups.size());
  const auto& tree = (*config.trained_trees)[0];
  EXPECT_EQ(vpt::pg_value(tree, q.data()), groups[0].lsh_bucket_id);
  EXPECT_EQ(0u, groups[0].hamming_distance);
  // Radius-1 probes come in increasing |d(q,p_R) - r_R| of the flipped
  // level (margins along the query's own path).
  std::vector<float> margins;
  vpt::descend(tree, q.data(), 0, &margins);
  float last = -1.0f;
  for (size_t i = 1; i < groups.size(); ++i) {
    const uint32_t diff = groups[i].lsh_bucket_id ^ groups[0].lsh_bucket_id;
    ASSERT_NE(0u, diff);
    const uint32_t level = __builtin_ctz(diff);  // lowest differing bit = flipped level
    EXPECT_GE(margins[level], last);
    last = margins[level];
  }
}

TEST(VectorPgLshTrainingDerived, NeedsTreesAndEncodesAsV4)
{
  const auto rows = make_data(2000, kDim, 51);
  auto config = td_config(rows);
  ceph::bufferlist bl;
  config.encode(bl);
  EXPECT_EQ(4, static_cast<uint8_t>(bl[0]));
  pg_lsh::index_config_t decoded;
  auto p = bl.cbegin();
  decoded.decode(p);
  EXPECT_EQ(pg_lsh::placement_mode_training_derived, decoded.placement_mode);
  EXPECT_FALSE(decoded.trained_trees);  // runtime-only, never encoded
  std::string mismatch;
  EXPECT_EQ(0, pg_lsh::compare_index_configs(config, decoded, &mismatch)) << mismatch;

  std::vector<vp::pg_lsh_v0_group_t> groups;
  EXPECT_EQ(-EINVAL, pg_lsh::exact_lsh_groups(to_bl(rows[0]), decoded, &groups));
  decoded.trained_trees = config.trained_trees;
  EXPECT_EQ(0, pg_lsh::exact_lsh_groups(to_bl(rows[0]), decoded, &groups));

  std::string field;
  config.hierarchical_scale = 1.0;
  EXPECT_EQ(-EINVAL, pg_lsh::validate_index_config(config, kPool, &field));
  EXPECT_EQ("hierarchical_scale", field);
}
