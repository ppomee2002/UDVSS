// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#include "librados/vector_placement.h"
#include "librados/vector_pg_lsh.h"
#include "include/rados.h"

#include <cmath>
#include <set>
#include <vector>

#include <gtest/gtest.h>

namespace vp = librados::vector_placement;

namespace {

constexpr uint32_t kDim = 16;
constexpr uint32_t kBits = 8;
constexpr uint32_t kTables = 4;
constexpr uint32_t kSeed = 1315423911;
constexpr uint32_t kPgNum = 1024;

ceph::bufferlist make_vector(int variant)
{
  std::vector<float> values(kDim);
  for (uint32_t i = 0; i < kDim; ++i) {
    values[i] = static_cast<float>(std::sin(0.7 * i + variant) * (i + 3));
  }
  ceph::bufferlist bl;
  bl.append(reinterpret_cast<const char*>(values.data()),
            values.size() * sizeof(float));
  return bl;
}

std::vector<float> to_floats(const ceph::bufferlist& bl)
{
  std::vector<float> values;
  EXPECT_EQ(0, vp::copy_float32_vector(bl, kDim, &values));
  return values;
}

} // namespace

TEST(VectorPgLshQueryRouting, DefaultsReproduceOriginalOrdering)
{
  const auto query = make_vector(1);
  std::vector<vp::pg_lsh_v0_group_t> original;
  ASSERT_EQ(0, vp::pg_lsh_v0_query_groups(
      query, kDim, kBits, kTables, 2, kSeed, &original));

  // Default routing must produce the same groups in the same order as
  // pg_lsh_v0_query_groups(), so existing callers are unaffected.
  std::vector<vp::pg_lsh_v0_group_t> routed;
  ASSERT_EQ(0, vp::pg_lsh_v0_query_groups_routed(
      query, kDim, kBits, kTables, 2, kSeed,
      vp::pg_lsh_v0_query_routing_t{}, &routed));

  ASSERT_EQ(original.size(), routed.size());
  for (size_t i = 0; i < original.size(); ++i) {
    EXPECT_EQ(original[i].table, routed[i].table) << "group " << i;
    EXPECT_EQ(original[i].lsh_bucket_id, routed[i].lsh_bucket_id) << "group " << i;
    EXPECT_EQ(original[i].hamming_distance, routed[i].hamming_distance)
        << "group " << i;
  }
}

TEST(VectorPgLshQueryRouting, TableCountRestrictsEnumeration)
{
  const auto query = make_vector(2);
  for (uint32_t tables = 1; tables <= kTables; ++tables) {
    vp::pg_lsh_v0_query_routing_t routing;
    routing.table_count = tables;
    std::vector<vp::pg_lsh_v0_group_t> routed;
    ASSERT_EQ(0, vp::pg_lsh_v0_query_groups_routed(
        query, kDim, kBits, kTables, 2, kSeed, routing, &routed));
    ASSERT_FALSE(routed.empty());
    for (const auto& group : routed) {
      EXPECT_LT(group.table, tables);
    }
  }

  // Restricting to more tables than the index has is a caller error, not a
  // silent clamp.
  vp::pg_lsh_v0_query_routing_t too_many;
  too_many.table_count = kTables + 1;
  std::vector<vp::pg_lsh_v0_group_t> routed;
  EXPECT_EQ(-EINVAL, vp::pg_lsh_v0_query_groups_routed(
      query, kDim, kBits, kTables, 2, kSeed, too_many, &routed));
}

TEST(VectorPgLshQueryRoutingWritePath, TableRestrictionStillReachesWritePg)
{
  // The point of --query-table-count: with write_pg_count = d, every vector
  // lands in a PG derived from tables [0, d). A query restricted to those
  // same tables must still be able to address that PG -- otherwise the
  // restriction would trade wasted probes for lost recall.
  for (int variant = 0; variant < 8; ++variant) {
    const auto vec = make_vector(variant);
    std::vector<vp::pg_lsh_v0_group_t> exact;
    ASSERT_EQ(0, vp::pg_lsh_v0_exact_groups(
        vec, kDim, kBits, kTables, kSeed, &exact));
    const auto write_pgs =
      vp::pg_lsh_v0_select_write_pgs(exact, kPgNum, kSeed, 1);
    ASSERT_EQ(1u, write_pgs.size());

    // d=1 always resolves to table 0: exact groups come out in table order
    // and the first unique PG wins.
    EXPECT_EQ(vp::pg_lsh_v0_group_to_pg(0, exact[0].lsh_bucket_id, kPgNum, kSeed),
              write_pgs[0]) << "variant " << variant;

    vp::pg_lsh_v0_query_routing_t routing;
    routing.table_count = 1;
    std::vector<vp::pg_lsh_v0_group_t> routed;
    ASSERT_EQ(0, vp::pg_lsh_v0_query_groups_routed(
        vec, kDim, kBits, kTables, 0, kSeed, routing, &routed));
    const auto ranked =
      vp::pg_lsh_v0_select_unique_pgs(routed, kPgNum, kSeed, 4);
    ASSERT_FALSE(ranked.empty());
    // Querying the vector itself, its own write PG must be rank 1.
    EXPECT_EQ(write_pgs[0], ranked.front().pg) << "variant " << variant;
  }
}

TEST(VectorPgLshQueryRouting, MarginOrderingIsAscendingInScore)
{
  const auto query = make_vector(3);
  const auto values = to_floats(query);

  vp::pg_lsh_v0_query_routing_t routing;
  routing.table_count = 1;
  routing.margin_ordered_probes = true;
  std::vector<vp::pg_lsh_v0_group_t> routed;
  ASSERT_EQ(0, vp::pg_lsh_v0_query_groups_routed(
      query, kDim, kBits, kTables, 3, kSeed, routing, &routed));
  ASSERT_GT(routed.size(), 8u);

  std::vector<double> projections;
  vp::pg_lsh_v0_bit_projections(values, 0, kBits, kSeed, &projections);
  const uint32_t exact =
    vp::pg_lsh_v0_lsh_bucket_id(values, 0, kBits, kSeed);

  // The exact bucket scores 0 and must lead.
  EXPECT_EQ(exact, routed.front().lsh_bucket_id);
  EXPECT_EQ(0u, routed.front().hamming_distance);

  double previous = -1.0;
  for (const auto& group : routed) {
    const double score =
      vp::pg_lsh_v0_mask_score(group.lsh_bucket_id ^ exact, projections);
    EXPECT_GE(score, previous - 1e-9)
        << "probe order must not decrease in score";
    previous = score;
  }

  // A cheap 2-bit flip can outrank an expensive 1-bit flip. That is the
  // point of the score ordering, so check it happens somewhere rather
  // than degenerating to hamming-distance order.
  bool saw_inversion = false;
  uint32_t seen_distance = 0;
  for (const auto& group : routed) {
    if (group.hamming_distance < seen_distance) {
      saw_inversion = true;
      break;
    }
    seen_distance = std::max(seen_distance, group.hamming_distance);
  }
  EXPECT_TRUE(saw_inversion)
      << "margin ordering never reordered across hamming levels";
}

TEST(VectorPgLshQueryRouting, MarginOrderingKeepsTheSameGroupSet)
{
  // Reordering must not add or drop probes -- only change their order.
  const auto query = make_vector(4);
  const auto key = [](const vp::pg_lsh_v0_group_t& g) {
    return std::pair<uint32_t, uint32_t>(g.table, g.lsh_bucket_id);
  };

  vp::pg_lsh_v0_query_routing_t plain;
  plain.table_count = 2;
  vp::pg_lsh_v0_query_routing_t margin = plain;
  margin.margin_ordered_probes = true;

  std::vector<vp::pg_lsh_v0_group_t> a, b;
  ASSERT_EQ(0, vp::pg_lsh_v0_query_groups_routed(
      query, kDim, kBits, kTables, 3, kSeed, plain, &a));
  ASSERT_EQ(0, vp::pg_lsh_v0_query_groups_routed(
      query, kDim, kBits, kTables, 3, kSeed, margin, &b));
  ASSERT_EQ(a.size(), b.size());

  std::set<std::pair<uint32_t, uint32_t>> set_a, set_b;
  for (const auto& g : a) set_a.insert(key(g));
  for (const auto& g : b) set_b.insert(key(g));
  EXPECT_EQ(set_a, set_b);
}

TEST(VectorPgLshQueryRouting, MarginOrderingIsDeterministic)
{
  const auto query = make_vector(5);
  vp::pg_lsh_v0_query_routing_t routing;
  routing.margin_ordered_probes = true;

  std::vector<vp::pg_lsh_v0_group_t> first, second;
  ASSERT_EQ(0, vp::pg_lsh_v0_query_groups_routed(
      query, kDim, kBits, kTables, 3, kSeed, routing, &first));
  ASSERT_EQ(0, vp::pg_lsh_v0_query_groups_routed(
      query, kDim, kBits, kTables, 3, kSeed, routing, &second));
  ASSERT_EQ(first.size(), second.size());
  for (size_t i = 0; i < first.size(); ++i) {
    EXPECT_EQ(first[i].table, second[i].table) << "group " << i;
    EXPECT_EQ(first[i].lsh_bucket_id, second[i].lsh_bucket_id) << "group " << i;
  }
}

TEST(VectorPgLshQueryRouting, BitProjectionSignsMatchBucketId)
{
  // pg_lsh_v0_bit_projections() and pg_lsh_v0_lsh_bucket_id() must agree
  // on which side of a hyperplane the query fell: the margin score only
  // means anything if it belongs to the bit that was chosen.
  for (int variant = 0; variant < 5; ++variant) {
    const auto values = to_floats(make_vector(variant));
    for (uint32_t table = 0; table < kTables; ++table) {
      std::vector<double> projections;
      vp::pg_lsh_v0_bit_projections(values, table, kBits, kSeed, &projections);
      const uint32_t bucket =
        vp::pg_lsh_v0_lsh_bucket_id(values, table, kBits, kSeed);
      for (uint32_t bit = 0; bit < kBits; ++bit) {
        const bool set = (bucket & (uint32_t{1} << bit)) != 0;
        EXPECT_EQ(set, projections[bit] >= 0)
            << "variant " << variant << " table " << table << " bit " << bit;
      }
    }
  }
}

TEST(VectorPgLshPartitions, WritePartitionFoldsToRoutedPg)
{
  // Partition mode writes at partition_num and routes at any smaller
  // power-of-two pg_num. That is only sound if folding the write
  // partition into pg_num gives the PG routing at pg_num would pick.
  constexpr uint32_t kPartitionNum = 1024;
  for (int variant = 0; variant < 64; ++variant) {
    std::vector<vp::pg_lsh_v0_group_t> groups;
    ASSERT_EQ(0, vp::pg_lsh_v0_exact_groups(
        make_vector(variant), kDim, kBits, kTables, kSeed, &groups));
    const auto partitions = vp::pg_lsh_v0_select_write_pgs(
        groups, kPartitionNum, kSeed, 1);
    ASSERT_EQ(1u, partitions.size());
    for (uint32_t pg_num = 1; pg_num <= kPartitionNum; pg_num <<= 1) {
      const auto routed =
        vp::pg_lsh_v0_select_write_pgs(groups, pg_num, kSeed, 1);
      ASSERT_EQ(1u, routed.size());
      EXPECT_EQ(routed.front(), partitions.front() & (pg_num - 1))
          << "variant " << variant << " pg_num " << pg_num;
    }
  }
}

TEST(VectorPgLshPartitions, FoldMatchesStableMod)
{
  namespace pg_lsh = librados::vector_pg_lsh;
  for (uint32_t pg_num : {1u, 3u, 64u, 96u, 100u, 128u, 1000u, 1024u}) {
    int mask = 1;
    while (static_cast<uint32_t>(mask) < pg_num) {
      mask <<= 1;
    }
    --mask;
    for (uint32_t i = 0; i < 4096; ++i) {
      const uint32_t hash = i * 2654435761u;
      EXPECT_EQ(static_cast<uint32_t>(ceph_stable_mod(
                    static_cast<int>(hash), static_cast<int>(pg_num), mask)),
                pg_lsh::fold_hash_to_pg(hash, pg_num))
          << "hash " << hash << " pg_num " << pg_num;
    }
  }
}

TEST(VectorPgLshPartitions, PartitionStaysInItsChildAcrossSplits)
{
  // A partition's locator hash has the partition id in its low bits, so
  // for every pg_num up to partition_num it lands in partition & (pg_num-1):
  // a split moves it to exactly the child routing expects.
  namespace pg_lsh = librados::vector_pg_lsh;
  constexpr uint32_t kPartitionNum = 256;
  std::vector<uint32_t> hashes(kPartitionNum);
  std::vector<bool> found(kPartitionNum, false);
  uint32_t found_count = 0;
  for (uint32_t salt = 0; found_count < kPartitionNum; ++salt) {
    const uint32_t hash = pg_lsh::partition_locator_hash(
        pg_lsh::pg_lsh_locator_key("pool", "b", "i", salt));
    const uint32_t partition = hash & (kPartitionNum - 1);
    if (!found[partition]) {
      found[partition] = true;
      hashes[partition] = hash;
      ++found_count;
    }
  }
  for (uint32_t partition = 0; partition < kPartitionNum; ++partition) {
    for (uint32_t pg_num = 1; pg_num <= kPartitionNum; pg_num <<= 1) {
      EXPECT_EQ(partition & (pg_num - 1),
                pg_lsh::fold_hash_to_pg(hashes[partition], pg_num));
    }
  }
}

TEST(VectorPgLshAlignedLocators, PgIsLowBitsOfLshBucketId)
{
  // Every salted locator's raw hash carries its bucket's LSH id in the low
  // k bits, so at any power-of-two pg_num <= 2^k the bucket sits in PG
  // lsh_id & (pg_num - 1), in the default namespace and in another one.
  namespace pg_lsh = librados::vector_pg_lsh;
  constexpr uint32_t k = 10;
  for (const std::string nspace : {"", "ns"}) {
    for (uint32_t table : {0u, 3u}) {
      for (uint32_t lsh_id = 0; lsh_id < (1u << k); ++lsh_id) {
        const uint32_t ordinal = pg_lsh::lsh_bucket_ordinal(table, lsh_id, k);
        std::string key;
        ASSERT_EQ(0, pg_lsh::find_lsh_aligned_locator(
            "b", "i", nspace, ordinal, k, &key));
        const uint32_t hash = pg_lsh::locator_raw_hash(nspace, key);
        for (uint32_t pg_num = 1; pg_num <= (1u << k); pg_num <<= 1) {
          ASSERT_EQ(lsh_id & (pg_num - 1),
                    pg_lsh::fold_hash_to_pg(hash, pg_num))
              << "ns '" << nspace << "' lsh_id " << lsh_id
              << " pg_num " << pg_num;
        }
      }
    }
  }
}

TEST(VectorPgLshAlignedLocators, SplitAddsOneSignatureBitMergeDropsIt)
{
  // Split N -> 2N: a bucket in PG p moves to p or p + N, picked by LSH bit
  // log2(N). Merge 2N -> N joins exactly the two buckets' PGs that differ
  // in that bit, i.e. Hamming-1 siblings.
  namespace pg_lsh = librados::vector_pg_lsh;
  constexpr uint32_t k = 10;
  std::vector<uint32_t> hashes(1u << k);
  for (uint32_t lsh_id = 0; lsh_id < (1u << k); ++lsh_id) {
    std::string key;
    ASSERT_EQ(0, pg_lsh::find_lsh_aligned_locator(
        "b", "i", "", lsh_id, k, &key));
    hashes[lsh_id] = pg_lsh::locator_raw_hash("", key);
  }
  for (uint32_t pg_num = 128; pg_num < (1u << k); pg_num <<= 1) {
    for (uint32_t lsh_id = 0; lsh_id < (1u << k); ++lsh_id) {
      const uint32_t parent = pg_lsh::fold_hash_to_pg(hashes[lsh_id], pg_num);
      const uint32_t child =
        pg_lsh::fold_hash_to_pg(hashes[lsh_id], pg_num * 2);
      const uint32_t new_bit = (lsh_id / pg_num) & 1;
      EXPECT_EQ(parent + new_bit * pg_num, child);
      const uint32_t sibling = lsh_id ^ pg_num;
      EXPECT_EQ(parent, pg_lsh::fold_hash_to_pg(hashes[sibling], pg_num));
      EXPECT_NE(child, pg_lsh::fold_hash_to_pg(hashes[sibling], pg_num * 2));
    }
  }
}

TEST(VectorPgLshAlignedLocators, ConfigValidation)
{
  namespace pg_lsh = librados::vector_pg_lsh;
  pg_lsh::index_config_t config;
  config.dimension = kDim;
  config.data_type = ceph::rados::vector_data_type_float32;
  config.distance_metric = ceph::rados::vector_distance_metric_euclidean;
  config.k = 10;
  config.l = 4;
  config.seed = kSeed;
  config.d = 1;
  config.anchor_mode = pg_lsh::anchor_mode_random;
  config.anchor.assign(kDim, 1.0);
  config.bucket_locators = pg_lsh::bucket_locators_lsh_aligned;
  pg_lsh::pool_pg_info_t pool{1024, 1024, 1};
  std::string field;
  EXPECT_EQ(0, pg_lsh::validate_index_config(config, pool, &field)) << field;
  pool.pg_num = pool.pgp_num = 2048;  // past 2^k
  EXPECT_EQ(-EINVAL, pg_lsh::validate_index_config(config, pool, &field));
  EXPECT_EQ("pg_num", field);
  pool.pg_num = pool.pgp_num = 768;   // not a power of two
  EXPECT_EQ(-EINVAL, pg_lsh::validate_index_config(config, pool, &field));
  EXPECT_EQ("pg_num", field);
  pool.pg_num = pool.pgp_num = 128;
  EXPECT_EQ(0, pg_lsh::validate_index_config(config, pool, &field)) << field;
  config.d = 2;                       // only table 0 is aligned
  EXPECT_EQ(-EINVAL, pg_lsh::validate_index_config(config, pool, &field));
  EXPECT_EQ("d", field);
  config.d = 1;
  config.bucket_locators = 3;
  EXPECT_EQ(-EINVAL, pg_lsh::validate_index_config(config, pool, &field));
  EXPECT_EQ("bucket_locators", field);
  config.bucket_locators = pg_lsh::bucket_locators_hashed;  // unchanged rules
  pool.pg_num = pool.pgp_num = 768;
  EXPECT_EQ(0, pg_lsh::validate_index_config(config, pool, &field)) << field;
}
