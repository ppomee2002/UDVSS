// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_LIBRADOS_VECTOR_PG_LSH_H
#define CEPH_LIBRADOS_VECTOR_PG_LSH_H

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include <errno.h>

#include "include/buffer.h"
#include "include/ceph_hash.h"
#include "include/encoding.h"
#include "include/object.h"
#include "include/rados.h"
#include "include/rados/librados.hpp"
#include "include/rados/vector_ops.h"
#include "librados/vector_put.h"
#include "librados/vector_placement.h"

namespace librados {
inline namespace v14_2_0 {
namespace vector_pg_lsh {

struct pool_pg_info_t {
  uint32_t pg_num = 0;
  uint32_t pgp_num = 0;
  uint64_t osdmap_epoch = 0;
};

inline constexpr uint32_t index_config_format_version = 1;
// "v0" identifies the first immutable PG-LSH placement/routing layout. A
// future incompatible mapping must use a new layout version and algorithm ID.
inline constexpr uint32_t pg_lsh_layout_version = 0;
// Second immutable layout. PG routing (k/l/d/seed) is identical to v0;
// only sub_oid distance_bucket placement differs, using raw Euclidean
// distance from the anchor instead of v0's normalized angular distance.
// Tree-boundary pruning needs distance_bucket and tau in the same metric
// space, which the normalized distance does not give. Requires
// distance_metric == euclidean and raw_distance_scale_max > 0; see
// validate_index_config(). v0 indexes keep their own layout version and
// are not affected.
inline constexpr uint32_t pg_lsh_layout_version_raw_euclidean_v1 = 1;

inline constexpr uint32_t bucket_locators_hashed = 1;
inline constexpr uint32_t bucket_locators_lsh_aligned = 2;

// How the k-bit LSH bucket id is derived. fixed_lsh is the original
// per-bit fixed random hyperplane signature (pg_lsh_v0_lsh_bucket_id()).
// hierarchical_lsh is the region-dependent binary partition of
// common/vector_pg_lsh_hierarchical.h, rooted at `anchor` with initial
// scale `hierarchical_scale`. training_derived is the same region-wise
// binary partition with trained per-region pivots and distance boundaries
// (common/vector_pivot_tree.h), supplied by the client in
// index_config_t::trained_trees. All feed the same PG masking, locators
// and split/merge handling; only the bucket id differs.
inline constexpr uint32_t placement_mode_fixed_lsh = 0;
inline constexpr uint32_t placement_mode_hierarchical_lsh = 1;
inline constexpr uint32_t placement_mode_training_derived = 2;

inline constexpr uint32_t anchor_mode_random = 1;
inline constexpr uint32_t anchor_mode_centroid = 2;
inline constexpr uint32_t anchor_mode_representative = 3;

// Immutable placement state. This object is encoded into the index metadata
// object and owns its anchor so put and query never regenerate routing state.
struct index_config_t {
  uint32_t config_format_version = index_config_format_version;
  std::string placement_algorithm =
    ceph::rados::vector_placement_algorithm_pg_lsh_v0;
  uint32_t placement_layout_version = pg_lsh_layout_version;
  uint32_t dimension = 0;
  uint32_t data_type = 0;
  uint32_t distance_metric = 0;
  uint32_t k = 0;
  uint32_t l = 0;
  uint32_t seed = 0;
  uint32_t d = 0;
  // pg-lsh-v0 maps LSH groups directly into the pool's PG number space.
  // Changing either value is unsupported because it changes existing routes.
  uint32_t creation_pg_num = 0;
  uint32_t creation_pgp_num = 0;
  uint32_t distance_bucket_bits = 0;
  uint32_t residual_bits = 0;
  uint32_t anchor_mode = 0;
  std::vector<double> anchor;
  // Quantization scale for the raw ||v-anchor||^2 -> distance_bucket
  // mapping. Must be > 0 for the raw-Euclidean layout and 0 for v0; see
  // validate_index_config(). Persisted so put and query agree on it.
  double raw_distance_scale_max = 0;
  // 0 keeps the original layout: LSH groups map straight into the pool's
  // PG space and each PG gets whichever locator currently hashes into it.
  // Nonzero (a power of two) fixes the partition space instead: vectors
  // are written to partition h % partition_num, and partition p's objects
  // carry a locator whose raw hash has p in its low bits. A pool with any
  // power-of-two pg_num <= partition_num then holds partition p in PG
  // p & (pg_num - 1), so a RADOS PG split moves whole partitions to the
  // same child that routing at the new pg_num expects, and no locator
  // changes when pg_num does.
  uint32_t partition_num = 0;
  // Nonzero (requires partition_num == 0): each LSH bucket (table,
  // bucket id) is one locator string, shared by the bucket's sub_oid
  // objects, and the bucket's routing hash IS that locator's RADOS hash.
  // A client routes a bucket to fold(hash, current pg_num) -- exactly
  // where RADOS stores it -- so a native split (one more hash bit) or
  // merge (one less) moves each bucket to the PG the client computes at
  // the new pg_num. Nothing about the load-time pg_num is kept.
  //
  // The value picks how a bucket's locator string is chosen:
  // bucket_locators_hashed keeps the plain per-bucket string, so the PG
  // is an rjenkins hash of the bucket id and PG siblings are unrelated
  // buckets. bucket_locators_lsh_aligned salts the string until its raw
  // hash carries the LSH bucket id in its low k bits, so the PG at any
  // power-of-two pg_num <= 2^k is the low log2(pg_num) bits of the LSH
  // signature: a split uses one more signature bit, a merge one less, and
  // the buckets a merge joins differ in exactly that one hyperplane.
  uint32_t bucket_locators = 0;
  // placement_mode_* above. 0 (fixed_lsh) is every index created before
  // this field existed.
  uint32_t placement_mode = placement_mode_fixed_lsh;
  // hierarchical_lsh only: S, the root offset scale. Level l moves the
  // child pivot by S / 2^(l+1) along h_R. Must be > 0 in hierarchical
  // mode and 0 otherwise; nothing derives it, so it is persisted here.
  double hierarchical_scale = 0;
  // training_derived only, runtime state: one trained tree per LSH table
  // (l trees of k levels). Never encoded or compared -- the encoded
  // config records only placement_mode, and every client must attach the
  // same trees (the bench retrains them deterministically and prints
  // their digest).
  std::shared_ptr<const std::vector<ceph::rados::vector_pivot_tree::tree_t>>
    trained_trees;

  bool has_v4_fields() const {
    return placement_mode != placement_mode_fixed_lsh ||
      hierarchical_scale != 0;
  }

  void encode(ceph::bufferlist& bl) const {
    // A fixed_lsh config encodes as v3, byte-identical to before
    // placement_mode existed. A hierarchical one encodes as v4 with
    // compat 4, so a decoder that predates it refuses the index rather
    // than routing it as fixed_lsh.
    const bool v4 = has_v4_fields();
    ENCODE_START(v4 ? 4 : 3, v4 ? 4 : 1, bl);
    using ceph::encode;
    encode(config_format_version, bl);
    encode(placement_algorithm, bl);
    encode(placement_layout_version, bl);
    encode(dimension, bl);
    encode(data_type, bl);
    encode(distance_metric, bl);
    encode(k, bl);
    encode(l, bl);
    encode(seed, bl);
    encode(d, bl);
    encode(creation_pg_num, bl);
    encode(creation_pgp_num, bl);
    encode(distance_bucket_bits, bl);
    encode(residual_bits, bl);
    encode(anchor_mode, bl);
    encode(anchor, bl);
    encode(raw_distance_scale_max, bl);
    encode(partition_num, bl);
    encode(bucket_locators, bl);
    if (v4) {
      encode(placement_mode, bl);
      encode(hierarchical_scale, bl);
    }
    ENCODE_FINISH(bl);
  }

  void decode(ceph::bufferlist::const_iterator& p) {
    DECODE_START(4, p);
    using ceph::decode;
    decode(config_format_version, p);
    decode(placement_algorithm, p);
    decode(placement_layout_version, p);
    decode(dimension, p);
    decode(data_type, p);
    decode(distance_metric, p);
    decode(k, p);
    decode(l, p);
    decode(seed, p);
    decode(d, p);
    decode(creation_pg_num, p);
    decode(creation_pgp_num, p);
    decode(distance_bucket_bits, p);
    decode(residual_bits, p);
    decode(anchor_mode, p);
    decode(anchor, p);
    decode(raw_distance_scale_max, p);
    if (struct_v >= 2) {
      decode(partition_num, p);
    } else {
      partition_num = 0;
    }
    if (struct_v >= 3) {
      decode(bucket_locators, p);
    } else {
      bucket_locators = 0;
    }
    if (struct_v >= 4) {
      decode(placement_mode, p);
      decode(hierarchical_scale, p);
    } else {
      placement_mode = placement_mode_fixed_lsh;
      hierarchical_scale = 0;
    }
    DECODE_FINISH(p);
  }
};

// Query-time tuning changes search volume only; it must not affect placement.
struct query_params_t {
  uint32_t hamming_radius = 0;
  uint32_t m = 0;
  uint32_t distance_bucket_radius = 0;
  uint32_t residual_hamming_radius = 0;
  uint32_t probe_limit_per_pg = 0;
  // Send one probe per selected PG (the query's exact sub_oid) and let
  // the OSD expand neighboring ONodes, instead of expanding the
  // Hamming ball on the client. Requires distance_bucket_radius and
  // residual_hamming_radius to be 0, since running both would scan
  // overlapping key space. PG selection is unaffected.
  bool server_side_boundary_search = false;
  // Max additional FLTree subtrees the OSD may expand beyond the primary
  // path, visited best-first; 0 disables it. Separate mechanism from
  // server_side_boundary_search above, which scans neighboring sub_oids
  // in a flat key range. The two are mutually exclusive.
  uint32_t tree_boundary_search_budget = 0;
  // Skip subtrees that cannot hold a result within tau, rather than only
  // reordering them. Requires tree_boundary_search_budget > 0 and the
  // raw-Euclidean layout, the only one where distance_bucket and tau
  // share a metric space. False keeps best-first ordering alone.
  bool tree_boundary_search_prune = false;
  // Consecutive non-improving subtree expansions tolerated before the OSD
  // stops expanding a probe. 0 stops at the first one.
  uint32_t tree_boundary_search_no_improve_patience = 0;
  // Max ONodes the OSD may open per probe; 0 is unlimited. Setting it
  // makes the search approximate: it caps cost, it does not bound how
  // much of the result set is missed.
  uint32_t tree_boundary_search_onode_budget = 0;
  // Query-side PG selection and ordering. Changes which PGs a query
  // visits, never where a vector is stored, so it can be changed on an
  // index that is already populated.
  vector_placement::pg_lsh_v0_query_routing_t query_routing;
  // bucket_locators indexes only: the current pg_num (a power of two),
  // i.e. pool_info.pg_num at query time. Sets the tree search band to the
  // whole probed PG, which may hold several LSH buckets.
  uint32_t band_pg_num = 0;
};

// Query callers normally provide no immutable overrides. These optionals are
// only for validating fields that a CLI user explicitly supplied.
struct index_config_overrides_t {
  std::optional<uint32_t> k;
  std::optional<uint32_t> l;
  std::optional<uint32_t> seed;
  std::optional<uint32_t> d;
  std::optional<uint32_t> distance_bucket_bits;
  std::optional<uint32_t> residual_bits;
  std::optional<uint32_t> anchor_mode;
};

struct locator_state_t {
  std::mutex lock;
  std::unordered_map<uint32_t, std::string> cache;
  uint32_t next_salt = 0;
  // Partition mode only: set once the pool is seen to place locators the
  // way partition_locator_hash() predicts.
  bool pool_hash_checked = false;
  // bucket_locators_lsh_aligned only: bucket ordinal -> its salted
  // locator key.
  std::unordered_map<uint32_t, std::string> aligned_cache;
};

inline bool is_power_of_two(uint32_t value)
{
  return value != 0 && (value & (value - 1)) == 0;
}

// The raw 32-bit placement hash RADOS computes for a locator key in the
// default namespace of an rjenkins pool, before it is folded into pg_num.
inline uint32_t partition_locator_hash(const std::string& locator_key)
{
  return ceph_str_hash_rjenkins(locator_key.data(), locator_key.size());
}

// Same fold as pg_pool_t::raw_hash_to_pg().
inline uint32_t fold_hash_to_pg(uint32_t hash, uint32_t pg_num)
{
  uint32_t mask = 1;
  while (mask < pg_num) {
    mask <<= 1;
  }
  --mask;
  return (hash & mask) < pg_num ? (hash & mask) : (hash & (mask >> 1));
}

// Checks that locator_key addresses logical PG/partition `pg`. The
// original layout asks the live OSDMap; partition mode only needs the low
// bits of the raw hash, which no pg_num change can alter.
inline int check_locator(v14_2_0::IoCtx& ioctx,
                         uint32_t pg,
                         uint32_t partition_num,
                         const std::string& locator_key)
{
  if (partition_num != 0) {
    return (partition_locator_hash(locator_key) & (partition_num - 1)) == pg
      ? 0 : -ESTALE;
  }
  uint32_t actual = 0;
  const int ret = ioctx.get_object_pg_hash_position2(locator_key, &actual);
  if (ret < 0) {
    return ret;
  }
  return actual == pg ? 0 : -ESTALE;
}

struct put_target_t {
  uint32_t pg = 0;
  object_t oid;
  std::string locator_key;
  std::string placement_key;
  std::string vector_hash;
  std::string sub_oid_name;
  // index_config_t::partition_num; pg above is a partition id when nonzero.
  uint32_t partition_num = 0;
};

struct query_probe_t {
  uint32_t pg = 0;
  uint32_t min_hamming_distance = 0;
  uint32_t table_vote_count = 0;
  object_t oid;
  std::string locator_key;
  std::string placement_key;
  std::string sub_oid_name;
  // index_config_t::partition_num; pg above is a partition id when nonzero.
  uint32_t partition_num = 0;
};

struct query_op_state_t {
  v14_2_0::IoCtx routed_ioctx;
  ::ObjectOperation op;
  ceph::bufferlist payload;
};

using put_op_state_t = vector_internal::put_op_state_t;

inline void set_mismatch_field(std::string *mismatch_field,
                               const char *field)
{
  if (mismatch_field != nullptr) {
    *mismatch_field = field;
  }
}

inline int validate_index_config(const index_config_t& config,
                                 const pool_pg_info_t& pool_info,
                                 std::string *invalid_field = nullptr)
{
  if (invalid_field != nullptr) {
    invalid_field->clear();
  }
  if (config.config_format_version != index_config_format_version) {
    set_mismatch_field(invalid_field, "config_format_version");
    return -EINVAL;
  }
  if (config.placement_algorithm !=
      ceph::rados::vector_placement_algorithm_pg_lsh_v0) {
    set_mismatch_field(invalid_field, "placement_algorithm");
    return -EINVAL;
  }
  if (config.placement_layout_version != pg_lsh_layout_version &&
      config.placement_layout_version !=
        pg_lsh_layout_version_raw_euclidean_v1) {
    set_mismatch_field(invalid_field, "placement_layout_version");
    return -EINVAL;
  }
  if (config.dimension == 0) {
    set_mismatch_field(invalid_field, "dimension");
    return -EINVAL;
  }
  size_t element_size = 0;
  if (ceph::rados::vector_data_type_size(
        config.data_type, &element_size) < 0) {
    set_mismatch_field(invalid_field, "data_type");
    return -EINVAL;
  }
  if (config.distance_metric != ceph::rados::vector_distance_metric_euclidean &&
      config.distance_metric != ceph::rados::vector_distance_metric_cosine &&
      config.distance_metric != ceph::rados::vector_distance_metric_dot) {
    set_mismatch_field(invalid_field, "distance_metric");
    return -EINVAL;
  }
  if (config.k == 0 ||
      config.k > ceph::rados::vector_lsh_v0_max_bits) {
    set_mismatch_field(invalid_field, "k");
    return -EINVAL;
  }
  if (config.l == 0 || config.l > 0xffffU) {
    set_mismatch_field(invalid_field, "l");
    return -EINVAL;
  }
  if (config.d == 0 || config.d > config.l) {
    set_mismatch_field(invalid_field, "d");
    return -EINVAL;
  }
  if (config.partition_num != 0) {
    // pool_info.pg_num is the resolution queries route at. It must divide
    // the partition space so each routed PG is a whole set of partitions.
    if (!is_power_of_two(config.partition_num) ||
        config.creation_pg_num != config.partition_num ||
        config.creation_pgp_num != config.partition_num) {
      set_mismatch_field(invalid_field, "partition_num");
      return -EINVAL;
    }
    if (!is_power_of_two(pool_info.pg_num) ||
        pool_info.pg_num > config.partition_num ||
        config.bucket_locators != 0) {
      set_mismatch_field(invalid_field, "pg_num");
      return -EINVAL;
    }
  } else if (config.bucket_locators != 0) {
    // Routing depends only on the current pg_num; nothing is pinned at
    // creation.
    if (config.creation_pg_num != 0 || config.creation_pgp_num != 0 ||
        pool_info.pg_num == 0) {
      set_mismatch_field(invalid_field, "pg_num");
      return -EINVAL;
    }
    if (config.bucket_locators != bucket_locators_hashed &&
        config.bucket_locators != bucket_locators_lsh_aligned) {
      set_mismatch_field(invalid_field, "bucket_locators");
      return -EINVAL;
    }
    if (config.bucket_locators == bucket_locators_lsh_aligned) {
      // The PG is the low log2(pg_num) bits of the table-0 signature, so
      // pg_num must be a power of two no larger than 2^k. Only table 0 is
      // aligned: a bucket of table t > 0 would share its PG with the
      // table-0 bucket of the same id, so data must stay in table 0.
      if (config.k > 20 || !is_power_of_two(pool_info.pg_num) ||
          pool_info.pg_num > (1u << config.k)) {
        set_mismatch_field(invalid_field, "pg_num");
        return -EINVAL;
      }
      if (config.d != 1) {
        set_mismatch_field(invalid_field, "d");
        return -EINVAL;
      }
    }
  } else {
    if (config.creation_pg_num == 0 ||
        config.creation_pg_num != pool_info.pg_num) {
      set_mismatch_field(invalid_field, "pg_num");
      return -EINVAL;
    }
    if (config.creation_pgp_num == 0 ||
        config.creation_pgp_num != pool_info.pgp_num) {
      set_mismatch_field(invalid_field, "pgp_num");
      return -EINVAL;
    }
  }
  if (config.distance_bucket_bits > 16) {
    set_mismatch_field(invalid_field, "distance_bucket_bits");
    return -EINVAL;
  }
  if (config.residual_bits > 16) {
    set_mismatch_field(invalid_field, "residual_bits");
    return -EINVAL;
  }
  if (config.anchor_mode != anchor_mode_random &&
      config.anchor_mode != anchor_mode_centroid &&
      config.anchor_mode != anchor_mode_representative) {
    set_mismatch_field(invalid_field, "anchor_mode");
    return -EINVAL;
  }
  if (config.anchor.size() != config.dimension) {
    set_mismatch_field(invalid_field, "anchor");
    return -EINVAL;
  }
  double anchor_norm_squared = 0;
  for (const double component : config.anchor) {
    if (!std::isfinite(component)) {
      set_mismatch_field(invalid_field, "anchor");
      return -EINVAL;
    }
    anchor_norm_squared += component * component;
  }
  if (anchor_norm_squared <= 0) {
    set_mismatch_field(invalid_field, "anchor");
    return -EINVAL;
  }
  if (config.placement_layout_version ==
      pg_lsh_layout_version_raw_euclidean_v1) {
    // tau is Euclidean regardless of distance_metric, so it only matches
    // the metric this index ranks by when that metric is euclidean.
    // Reject cosine/dot here rather than mis-scoring at query time.
    if (config.distance_metric != ceph::rados::vector_distance_metric_euclidean) {
      set_mismatch_field(invalid_field, "distance_metric");
      return -EINVAL;
    }
    if (!(config.raw_distance_scale_max > 0) ||
        !std::isfinite(config.raw_distance_scale_max)) {
      set_mismatch_field(invalid_field, "raw_distance_scale_max");
      return -EINVAL;
    }
  } else if (config.raw_distance_scale_max != 0) {
    // v0's normalized angular geometry has no scale parameter, so a
    // nonzero value here usually means the caller forgot to set
    // placement_layout_version.
    set_mismatch_field(invalid_field, "raw_distance_scale_max");
    return -EINVAL;
  }
  if (config.placement_mode == placement_mode_hierarchical_lsh) {
    if (!(config.hierarchical_scale > 0) ||
        !std::isfinite(config.hierarchical_scale)) {
      set_mismatch_field(invalid_field, "hierarchical_scale");
      return -EINVAL;
    }
  } else if (config.placement_mode == placement_mode_training_derived) {
    if (config.hierarchical_scale != 0) {
      set_mismatch_field(invalid_field, "hierarchical_scale");
      return -EINVAL;
    }
  } else if (config.placement_mode != placement_mode_fixed_lsh) {
    set_mismatch_field(invalid_field, "placement_mode");
    return -EINVAL;
  } else if (config.hierarchical_scale != 0) {
    // fixed_lsh has no pivots; a scale here means placement_mode was
    // left unset.
    set_mismatch_field(invalid_field, "hierarchical_scale");
    return -EINVAL;
  }
  if (config.d > (config.partition_num != 0 ? config.partition_num
                                            : pool_info.pg_num)) {
    set_mismatch_field(invalid_field, "d");
    return -EINVAL;
  }
  return 0;
}

inline int validate_query_params(const index_config_t& config,
                                 const query_params_t& query_params,
                                 const pool_pg_info_t& pool_info,
                                 std::string *invalid_field = nullptr)
{
  int ret = validate_index_config(config, pool_info, invalid_field);
  if (ret < 0) {
    return ret;
  }
  if (query_params.hamming_radius > config.k) {
    set_mismatch_field(invalid_field, "hamming_radius");
    return -EINVAL;
  }
  if (query_params.m == 0 || query_params.m > pool_info.pg_num) {
    set_mismatch_field(invalid_field, "m");
    return -EINVAL;
  }
  if (query_params.residual_hamming_radius > config.residual_bits) {
    set_mismatch_field(invalid_field, "residual_hamming_radius");
    return -EINVAL;
  }
  if (query_params.band_pg_num != 0 &&
      (config.bucket_locators == 0 ||
       query_params.band_pg_num != pool_info.pg_num ||
       !is_power_of_two(query_params.band_pg_num))) {
    set_mismatch_field(invalid_field, "band_pg_num");
    return -EINVAL;
  }
  if (query_params.query_routing.table_count > config.l) {
    // Restricting to more tables than the index has is a caller mistake,
    // not a silently clamped no-op.
    set_mismatch_field(invalid_field, "query_routing_table_count");
    return -EINVAL;
  }
  if (config.distance_bucket_bits == 0 &&
      query_params.distance_bucket_radius != 0) {
    set_mismatch_field(invalid_field, "distance_bucket_radius");
    return -EINVAL;
  }
  if (!vector_placement::pg_lsh_v0::sub_oid_enabled(
        config.distance_bucket_bits, config.residual_bits) &&
      query_params.probe_limit_per_pg != 0) {
    set_mismatch_field(invalid_field, "probe_limit_per_pg");
    return -EINVAL;
  }
  if (query_params.server_side_boundary_search) {
    // Client-side Hamming-ball expansion and the OSD's own expansion
    // must not both run against the same PG.
    if (query_params.distance_bucket_radius != 0) {
      set_mismatch_field(invalid_field, "distance_bucket_radius");
      return -EINVAL;
    }
    if (query_params.residual_hamming_radius != 0) {
      set_mismatch_field(invalid_field, "residual_hamming_radius");
      return -EINVAL;
    }
    if (!vector_placement::pg_lsh_v0::sub_oid_enabled(
          config.distance_bucket_bits, config.residual_bits)) {
      set_mismatch_field(invalid_field, "server_side_boundary_search");
      return -EINVAL;
    }
  }
  if (query_params.tree_boundary_search_budget != 0 &&
      query_params.server_side_boundary_search) {
    // Two independent boundary-search mechanisms; never combine them on
    // the same query.
    set_mismatch_field(invalid_field, "tree_boundary_search_budget");
    return -EINVAL;
  }
  if (query_params.tree_boundary_search_prune) {
    if (query_params.tree_boundary_search_budget == 0) {
      set_mismatch_field(invalid_field, "tree_boundary_search_prune");
      return -EINVAL;
    }
    if (config.placement_layout_version !=
        pg_lsh_layout_version_raw_euclidean_v1) {
      // Pruning needs distance_bucket and tau in the same metric space,
      // which only the raw-Euclidean layout provides. Reject rather than
      // falling back to best-first-only on the OSD.
      set_mismatch_field(invalid_field, "tree_boundary_search_prune");
      return -EINVAL;
    }
  }
  return 0;
}

inline int compare_index_configs(const index_config_t& requested,
                                 const index_config_t& stored,
                                 std::string *mismatch_field)
{
#define CHECK_INDEX_CONFIG_FIELD(field) \
  do { \
    if (requested.field != stored.field) { \
      set_mismatch_field(mismatch_field, #field); \
      return -EINVAL; \
    } \
  } while (false)
  CHECK_INDEX_CONFIG_FIELD(config_format_version);
  CHECK_INDEX_CONFIG_FIELD(placement_algorithm);
  CHECK_INDEX_CONFIG_FIELD(placement_layout_version);
  CHECK_INDEX_CONFIG_FIELD(dimension);
  CHECK_INDEX_CONFIG_FIELD(data_type);
  CHECK_INDEX_CONFIG_FIELD(distance_metric);
  CHECK_INDEX_CONFIG_FIELD(k);
  CHECK_INDEX_CONFIG_FIELD(l);
  CHECK_INDEX_CONFIG_FIELD(seed);
  CHECK_INDEX_CONFIG_FIELD(d);
  if (requested.creation_pg_num != stored.creation_pg_num) {
    set_mismatch_field(mismatch_field, "pg_num");
    return -EINVAL;
  }
  if (requested.creation_pgp_num != stored.creation_pgp_num) {
    set_mismatch_field(mismatch_field, "pgp_num");
    return -EINVAL;
  }
  CHECK_INDEX_CONFIG_FIELD(distance_bucket_bits);
  CHECK_INDEX_CONFIG_FIELD(residual_bits);
  CHECK_INDEX_CONFIG_FIELD(anchor_mode);
  CHECK_INDEX_CONFIG_FIELD(anchor);
  CHECK_INDEX_CONFIG_FIELD(raw_distance_scale_max);
  CHECK_INDEX_CONFIG_FIELD(partition_num);
  CHECK_INDEX_CONFIG_FIELD(bucket_locators);
  CHECK_INDEX_CONFIG_FIELD(placement_mode);
  CHECK_INDEX_CONFIG_FIELD(hierarchical_scale);
#undef CHECK_INDEX_CONFIG_FIELD
  if (mismatch_field != nullptr) {
    mismatch_field->clear();
  }
  return 0;
}

inline int validate_index_config_overrides(
    const index_config_overrides_t& overrides,
    const index_config_t& stored,
    std::string *mismatch_field)
{
#define CHECK_INDEX_CONFIG_OVERRIDE(field) \
  do { \
    if (overrides.field && *overrides.field != stored.field) { \
      set_mismatch_field(mismatch_field, #field); \
      return -EINVAL; \
    } \
  } while (false)
  CHECK_INDEX_CONFIG_OVERRIDE(k);
  CHECK_INDEX_CONFIG_OVERRIDE(l);
  CHECK_INDEX_CONFIG_OVERRIDE(seed);
  CHECK_INDEX_CONFIG_OVERRIDE(d);
  CHECK_INDEX_CONFIG_OVERRIDE(distance_bucket_bits);
  CHECK_INDEX_CONFIG_OVERRIDE(residual_bits);
  CHECK_INDEX_CONFIG_OVERRIDE(anchor_mode);
#undef CHECK_INDEX_CONFIG_OVERRIDE
  if (mismatch_field != nullptr) {
    mismatch_field->clear();
  }
  return 0;
}

inline int validate_request_layout(const index_config_t& config,
                                   uint32_t data_type,
                                   uint32_t distance_metric,
                                   uint32_t dimension,
                                   std::string *mismatch_field)
{
  if (data_type != config.data_type) {
    set_mismatch_field(mismatch_field, "data_type");
    return -EINVAL;
  }
  if (distance_metric != config.distance_metric) {
    set_mismatch_field(mismatch_field, "distance_metric");
    return -EINVAL;
  }
  if (dimension != config.dimension) {
    set_mismatch_field(mismatch_field, "dimension");
    return -EINVAL;
  }
  if (mismatch_field != nullptr) {
    mismatch_field->clear();
  }
  return 0;
}

inline vector_placement::pg_lsh_v0::sub_oid_config_t sub_oid_config_view(
    const index_config_t& config)
{
  return {
    config.dimension,
    config.seed,
    config.distance_bucket_bits,
    config.residual_bits,
    std::span<const double>(config.anchor),
    config.placement_layout_version == pg_lsh_layout_version_raw_euclidean_v1
      ? ceph::rados::vector_pg_lsh_placement::distance_geometry_raw_euclidean_v1
      : ceph::rados::vector_pg_lsh_placement::distance_geometry_normalized_angular_v0,
    config.raw_distance_scale_max,
  };
}

inline int compute_sub_oid(
    const ceph::bufferlist& vector_data,
    const index_config_t& config,
    vector_placement::pg_lsh_v0::sub_oid_t *out_sub_oid)
{
  return vector_placement::pg_lsh_v0::compute_sub_oid(
      vector_data, sub_oid_config_view(config), out_sub_oid);
}

inline int load_index_config(v14_2_0::IoCtx& ioctx,
                             const std::string& bucket_name,
                             const std::string& index_name,
                             index_config_t *config,
                             std::string *invalid_field = nullptr)
{
  if (config == nullptr || bucket_name.empty() || index_name.empty()) {
    return -EINVAL;
  }
  if (invalid_field != nullptr) {
    invalid_field->clear();
  }

  ceph::bufferlist encoded;
  const object_t metadata_oid =
    vector_placement::make_pg_lsh_index_metadata_oid(
        bucket_name, index_name);
  int ret = ioctx.read(metadata_oid.name, encoded, 0, 0);
  if (ret < 0) {
    if (ret == -ENOENT) {
      set_mismatch_field(invalid_field, "index_metadata");
    }
    return ret;
  }

  try {
    auto p = encoded.cbegin();
    config->decode(p);
    if (!p.end()) {
      set_mismatch_field(invalid_field, "trailing_metadata");
      return -EIO;
    }
  } catch (const ceph::buffer::error&) {
    set_mismatch_field(invalid_field, "encoded_metadata");
    return -EIO;
  }
  return 0;
}

inline int create_index(v14_2_0::IoCtx& ioctx,
                        const std::string& bucket_name,
                        const std::string& index_name,
                        const index_config_t& requested_config,
                        const pool_pg_info_t& pool_info,
                        std::string *mismatch_field = nullptr)
{
  if (bucket_name.empty() || index_name.empty()) {
    return -EINVAL;
  }
  int ret = validate_index_config(
      requested_config, pool_info, mismatch_field);
  if (ret < 0) {
    return ret;
  }

  ceph::bufferlist encoded;
  requested_config.encode(encoded);
  ObjectWriteOperation op;
  op.create(true);
  op.write_full(encoded);
  const object_t metadata_oid =
    vector_placement::make_pg_lsh_index_metadata_oid(
        bucket_name, index_name);
  ret = ioctx.operate(metadata_oid.name, &op);
  if (ret == 0) {
    if (mismatch_field != nullptr) {
      mismatch_field->clear();
    }
    return 0;
  }
  if (ret != -EEXIST) {
    return ret;
  }

  index_config_t stored_config;
  ret = load_index_config(
      ioctx, bucket_name, index_name, &stored_config, mismatch_field);
  if (ret < 0) {
    return ret;
  }
  return compare_index_configs(
      requested_config, stored_config, mismatch_field);
}

// Candidate locator key number `salt` for an index; locator_cache_t keeps
// the first salt that lands in each PG/partition.
inline std::string pg_lsh_locator_key(const std::string& pool_name,
                                      const std::string& bucket_hash,
                                      const std::string& index_hash,
                                      uint32_t salt)
{
  return "pg-lsh-v0:" + pool_name + ":" + bucket_hash + ":" + index_hash +
    ":salt:" + std::to_string(salt);
}

// bucket_locators: one LSH bucket = one locator key, shared by all of the
// bucket's sub_oid objects. Its RADOS placement hash is whatever
// pg_pool_t::hash_key() makes of this string; nothing here computes it.
inline uint32_t lsh_bucket_ordinal(uint32_t table, uint32_t lsh_bucket_id,
                                   uint32_t k)
{
  return (table << k) | lsh_bucket_id;
}

inline std::string lsh_bucket_locator_key(const std::string& bucket_hash,
                                          const std::string& index_hash,
                                          uint32_t ordinal)
{
  return "pg-lsh-bucket:" + bucket_hash + ":" + index_hash + ":" +
    std::to_string(ordinal);
}

// Same input pg_pool_t::hash_key() feeds an rjenkins pool: the key alone
// in the default namespace, otherwise namespace + '\037' + key.
inline uint32_t locator_raw_hash(const std::string& nspace,
                                 const std::string& locator_key)
{
  if (nspace.empty()) {
    return partition_locator_hash(locator_key);
  }
  const std::string input = nspace + '\037' + locator_key;
  return ceph_str_hash_rjenkins(input.data(), input.size());
}

inline std::string lsh_aligned_locator_key(const std::string& bucket_hash,
                                           const std::string& index_hash,
                                           uint32_t ordinal,
                                           uint32_t salt)
{
  return "pg-lsh-aligned:" + bucket_hash + ":" + index_hash + ":" +
    std::to_string(ordinal) + ":salt:" + std::to_string(salt);
}

// bucket_locators_lsh_aligned: the first salted key for bucket `ordinal`
// whose raw hash has the bucket's LSH id (the ordinal's low `lsh_bits`
// bits) in its low `lsh_bits` bits. RADOS folds that hash into PG
// hash & (pg_num - 1) for a power-of-two pg_num, so the PG is the low
// log2(pg_num) bits of the LSH id for every pg_num <= 2^lsh_bits. The key
// depends only on the names and the ordinal, so every client and every
// pg_num agree on it. About 2^lsh_bits tries per bucket.
inline int find_lsh_aligned_locator(const std::string& bucket_hash,
                                    const std::string& index_hash,
                                    const std::string& nspace,
                                    uint32_t ordinal,
                                    uint32_t lsh_bits,
                                    std::string *locator_key)
{
  if (locator_key == nullptr || lsh_bits == 0 || lsh_bits > 20) {
    return -EINVAL;
  }
  const uint32_t mask = (1u << lsh_bits) - 1;
  const uint32_t target = ordinal & mask;
  const uint32_t search_limit = 64u << lsh_bits;
  for (uint32_t salt = 0; salt < search_limit; ++salt) {
    std::string candidate =
      lsh_aligned_locator_key(bucket_hash, index_hash, ordinal, salt);
    if ((locator_raw_hash(nspace, candidate) & mask) == target) {
      *locator_key = std::move(candidate);
      return 0;
    }
  }
  return -ENOENT;
}

class locator_cache_t {
 public:
  locator_cache_t(v14_2_0::IoCtx *ioctx,
                  pool_pg_info_t info,
                  std::string pool_name,
                  std::string bucket_name,
                  std::string index_name,
                  std::shared_ptr<locator_state_t> state = nullptr,
                  uint32_t partition_num = 0,
                  uint32_t bucket_locators = 0,
                  uint32_t lsh_bits = 0)
    : ioctx(ioctx),
      info(info),
      pool_name(std::move(pool_name)),
      bucket_hash(vector_placement::hash_string(bucket_name)),
      index_hash(vector_placement::hash_string(index_name)),
      state(state ? std::move(state) : std::make_shared<locator_state_t>()),
      partition_num(partition_num),
      bucket_locators(bucket_locators),
      lsh_bits(lsh_bits) {}

  int precompute_all()
  {
    if (ioctx == nullptr || info.pg_num == 0) {
      return -EINVAL;
    }
    if (bucket_locators == bucket_locators_lsh_aligned &&
        (lsh_bits == 0 || lsh_bits > 20 || !is_power_of_two(info.pg_num) ||
         info.pg_num > (1u << lsh_bits))) {
      return -EINVAL;
    }
    if (bucket_locators) {
      return check_current_pg_num();
    }
    if (partition_num != 0) {
      return precompute_partitions();
    }

    std::lock_guard locker(state->lock);
    const uint32_t search_limit =
      std::max<uint32_t>(10000000U, info.pg_num * 1024U);
    while (state->cache.size() < info.pg_num &&
           state->next_salt < search_limit) {
      std::string candidate = make_locator_key(state->next_salt++);
      uint32_t actual = 0;
      const int ret =
        ioctx->get_object_pg_hash_position2(candidate, &actual);
      if (ret < 0) {
        return ret;
      }
      if (actual < info.pg_num &&
          state->cache.find(actual) == state->cache.end()) {
        state->cache.emplace(actual, std::move(candidate));
      }
    }

    if (state->cache.size() != info.pg_num) {
      return -ENOENT;
    }

    for (uint32_t pg = 0; pg < info.pg_num; ++pg) {
      const auto found = state->cache.find(pg);
      if (found == state->cache.end()) {
        return -ENOENT;
      }
      uint32_t actual = 0;
      const int ret =
        ioctx->get_object_pg_hash_position2(found->second, &actual);
      if (ret < 0) {
        return ret;
      }
      if (actual != pg) {
        return -ESTALE;
      }
    }
    return 0;
  }

  int locator_for_pg(uint32_t pg, std::string *locator_key)
  {
    if (locator_key == nullptr || ioctx == nullptr || pg >= address_space()) {
      return -EINVAL;
    }

    std::string found_locator;
    {
      std::lock_guard locker(state->lock);
      const auto found = state->cache.find(pg);
      if (found == state->cache.end()) {
        return -ENOENT;
      }
      found_locator = found->second;
    }

    const int ret = verify_locator(pg, found_locator);
    if (ret < 0) {
      return ret;
    }
    *locator_key = std::move(found_locator);
    return 0;
  }

  // bucket_locators: the locator key of LSH bucket `ordinal` and the PG
  // RADOS maps it to under the current OSDMap. The PG comes from
  // Objecter::get_object_pg_hash_position(), i.e.
  // pg_pool_t::raw_hash_to_pg(pg_pool_t::hash_key(key)) =
  // ceph_stable_mod(ceph_str_hash(key), pg_num, pg_num_mask): the same
  // computation that places the object, so a split or merge changes the
  // answer exactly as it moves the object.
  int bucket_locator(uint32_t ordinal, std::string *locator_key,
                     uint32_t *pg) const
  {
    if (locator_key == nullptr || pg == nullptr || ioctx == nullptr ||
        !bucket_locators) {
      return -EINVAL;
    }
    if (bucket_locators != bucket_locators_lsh_aligned) {
      *locator_key = lsh_bucket_locator_key(bucket_hash, index_hash, ordinal);
      return ioctx->get_object_pg_hash_position2(*locator_key, pg);
    }

    int ret = aligned_locator(ordinal, locator_key);
    if (ret < 0) {
      return ret;
    }
    ret = ioctx->get_object_pg_hash_position2(*locator_key, pg);
    if (ret < 0) {
      return ret;
    }
    // The PG still comes from the OSDMap. It must be the low bits of the
    // LSH id; anything else means the pool does not hash the way
    // locator_raw_hash() assumes (not rjenkins) or pg_num is not the
    // power of two this cache was built for.
    const uint32_t lsh_id = ordinal & ((1u << lsh_bits) - 1);
    if (*pg != (lsh_id & (info.pg_num - 1))) {
      return -EOPNOTSUPP;
    }
    return 0;
  }

  int verify_locator(uint32_t pg, const std::string& locator_key) const
  {
    if (ioctx == nullptr || pg >= address_space() || locator_key.empty()) {
      return -EINVAL;
    }
    return check_locator(*ioctx, pg, partition_num, locator_key);
  }

 private:
  int aligned_locator(uint32_t ordinal, std::string *locator_key) const
  {
    {
      std::lock_guard locker(state->lock);
      const auto found = state->aligned_cache.find(ordinal);
      if (found != state->aligned_cache.end()) {
        *locator_key = found->second;
        return 0;
      }
    }
    const int ret = find_lsh_aligned_locator(
        bucket_hash, index_hash, ioctx->get_namespace(), ordinal, lsh_bits,
        locator_key);
    if (ret < 0) {
      return ret;
    }
    std::lock_guard locker(state->lock);
    state->aligned_cache.emplace(ordinal, *locator_key);
    return 0;
  }

  uint32_t address_space() const
  {
    return partition_num != 0 ? partition_num : info.pg_num;
  }

  // bucket_locators: the caller's pg_num (info.pg_num, used for the tree
  // search band) must be the pool's current one. Checked against Ceph's
  // own numbers on sample keys: the raw hash from
  // get_object_hash_position2() (pg_pool_t::hash_key) folded with
  // ceph_stable_mod() at info.pg_num must equal the PG from
  // get_object_pg_hash_position2() (pg_pool_t::raw_hash_to_pg). A stale
  // pg_num fails this for most keys.
  int check_current_pg_num() const
  {
    int mask = 1;
    while (static_cast<uint32_t>(mask) < info.pg_num) {
      mask <<= 1;
    }
    --mask;
    for (uint32_t i = 0; i < 256; ++i) {
      const std::string key = lsh_bucket_locator_key(
          bucket_hash, index_hash, 0x80000000U | i);
      uint32_t raw = 0, pg = 0;
      int ret = ioctx->get_object_hash_position2(key, &raw);
      if (ret == 0) {
        ret = ioctx->get_object_pg_hash_position2(key, &pg);
      }
      if (ret < 0) {
        return ret;
      }
      if (static_cast<uint32_t>(ceph_stable_mod(
            static_cast<int>(raw), static_cast<int>(info.pg_num), mask)) != pg) {
        return -ESTALE;
      }
    }
    return 0;
  }

  // Partition mode: the salt for partition p is the first one whose raw
  // hash has p in its low bits. That depends only on the locator string,
  // so every client finds the same salt whatever the pool's pg_num is now.
  int precompute_partitions()
  {
    if (!is_power_of_two(partition_num)) {
      return -EINVAL;
    }
    std::lock_guard locker(state->lock);
    const uint32_t mask = partition_num - 1;
    const uint32_t search_limit =
      std::max<uint32_t>(10000000U, partition_num * 1024U);
    while (state->cache.size() < partition_num &&
           state->next_salt < search_limit) {
      std::string candidate = make_locator_key(state->next_salt++);
      const uint32_t partition = partition_locator_hash(candidate) & mask;
      if (state->cache.find(partition) == state->cache.end()) {
        state->cache.emplace(partition, std::move(candidate));
      }
    }
    if (state->cache.size() != partition_num) {
      return -ENOENT;
    }
    if (state->pool_hash_checked) {
      return 0;
    }

    // partition_locator_hash() assumes an rjenkins pool and the default
    // namespace. Confirm it against the OSDMap: every locator must fold
    // into the same PG the pool reports. The pool's pg_num is taken as the
    // smallest one consistent with the positions seen, which is exact
    // whenever pg_num <= partition_num (every residue is then present).
    std::vector<std::pair<uint32_t, uint32_t>> hash_and_actual;
    hash_and_actual.reserve(partition_num);
    uint32_t max_actual = 0;
    for (const auto& [partition, locator_key] : state->cache) {
      uint32_t actual = 0;
      const int ret =
        ioctx->get_object_pg_hash_position2(locator_key, &actual);
      if (ret < 0) {
        return ret;
      }
      hash_and_actual.emplace_back(partition_locator_hash(locator_key), actual);
      max_actual = std::max(max_actual, actual);
    }
    const uint32_t live_pg_num = max_actual + 1;
    if (live_pg_num > partition_num) {
      return -EOPNOTSUPP;
    }
    for (const auto& [hash, actual] : hash_and_actual) {
      if (fold_hash_to_pg(hash, live_pg_num) != actual) {
        return -EOPNOTSUPP;
      }
    }
    state->pool_hash_checked = true;
    return 0;
  }

  std::string make_locator_key(uint32_t salt) const
  {
    return pg_lsh_locator_key(pool_name, bucket_hash, index_hash, salt);
  }

  v14_2_0::IoCtx *ioctx = nullptr;
  pool_pg_info_t info;
  std::string pool_name;
  std::string bucket_hash;
  std::string index_hash;
  std::shared_ptr<locator_state_t> state;
  uint32_t partition_num = 0;
  uint32_t bucket_locators = 0;
  uint32_t lsh_bits = 0;
};

// The LSH groups a vector is written to, under the index's placement_mode.
// Put (select_write_pgs(), build_put_targets()) and the bench audit call
// this, and query_lsh_groups() below derives its exact groups the same
// way, so put and query always agree on the bucket id.
inline vector_placement::pg_lsh_hier_params_t hierarchical_params(
    const index_config_t& config)
{
  return {
    config.dimension,
    config.k,
    config.l,
    config.seed,
    std::span<const double>(config.anchor),
    config.hierarchical_scale,
  };
}

inline vector_placement::pg_lsh_td_params_t training_derived_params(
    const index_config_t& config)
{
  return {
    config.dimension,
    config.k,
    config.l,
    config.trained_trees.get(),
  };
}

inline int exact_lsh_groups(
    const ceph::bufferlist& vector_data,
    const index_config_t& config,
    std::vector<vector_placement::pg_lsh_v0_group_t> *groups)
{
  if (config.placement_mode == placement_mode_training_derived) {
    return vector_placement::pg_lsh_td_exact_groups(
        vector_data, training_derived_params(config), groups);
  }
  if (config.placement_mode == placement_mode_hierarchical_lsh) {
    return vector_placement::pg_lsh_hier_exact_groups(
        vector_data, hierarchical_params(config), groups);
  }
  return vector_placement::pg_lsh_v0_exact_groups(
      vector_data, config.dimension, config.k, config.l, config.seed,
      groups);
}

inline int query_lsh_groups(
    const ceph::bufferlist& query_vector,
    const index_config_t& config,
    uint32_t hamming_radius,
    const vector_placement::pg_lsh_v0_query_routing_t& routing,
    std::vector<vector_placement::pg_lsh_v0_group_t> *groups)
{
  if (config.placement_mode == placement_mode_training_derived) {
    return vector_placement::pg_lsh_td_query_groups_routed(
        query_vector, training_derived_params(config), hamming_radius,
        routing, groups);
  }
  if (config.placement_mode == placement_mode_hierarchical_lsh) {
    return vector_placement::pg_lsh_hier_query_groups_routed(
        query_vector, hierarchical_params(config), hamming_radius, routing,
        groups);
  }
  return vector_placement::pg_lsh_v0_query_groups_routed(
      query_vector, config.dimension, config.k, config.l, hamming_radius,
      config.seed, routing, groups);
}

inline int select_write_pgs(const ceph::bufferlist& vector_data,
                            const index_config_t& config,
                            const pool_pg_info_t& pool_info,
                            std::vector<uint32_t> *write_pgs)
{
  if (write_pgs == nullptr) {
    return -EINVAL;
  }
  write_pgs->clear();

  int ret = validate_index_config(config, pool_info);
  if (ret < 0) {
    return ret;
  }

  std::vector<vector_placement::pg_lsh_v0_group_t> exact_groups;
  ret = exact_lsh_groups(vector_data, config, &exact_groups);
  if (ret < 0) {
    return ret;
  }

  // Partition mode writes at partition resolution so the data never
  // depends on the pool's current pg_num.
  const uint32_t write_space =
    config.partition_num != 0 ? config.partition_num : pool_info.pg_num;
  *write_pgs = vector_placement::pg_lsh_v0_select_write_pgs(
      exact_groups, write_space, config.seed, config.d);
  return 0;
}

inline int select_query_pgs(
    const ceph::bufferlist& query_vector,
    const index_config_t& config,
    const query_params_t& query_params,
    const pool_pg_info_t& pool_info,
    std::vector<vector_placement::pg_lsh_v0_ranked_pg_t> *query_pgs,
    uint64_t *generated_group_count)
{
  if (query_pgs == nullptr) {
    return -EINVAL;
  }
  query_pgs->clear();
  if (generated_group_count != nullptr) {
    *generated_group_count = 0;
  }

  int ret = validate_query_params(config, query_params, pool_info);
  if (ret < 0) {
    return ret;
  }

  std::vector<vector_placement::pg_lsh_v0_group_t> groups;
  ret = query_lsh_groups(
      query_vector, config, query_params.hamming_radius,
      query_params.query_routing, &groups);
  if (ret < 0) {
    return ret;
  }
  if (generated_group_count != nullptr) {
    *generated_group_count = groups.size();
  }

  *query_pgs = vector_placement::pg_lsh_v0_select_unique_pgs(
      groups, pool_info.pg_num, config.seed, query_params.m);
  return 0;
}

inline int build_put_targets(const std::string& bucket_name,
                             const std::string& index_name,
                             const ceph::bufferlist& vector_data,
                             const index_config_t& config,
                             const pool_pg_info_t& pool_info,
                             locator_cache_t *locator_cache,
                             std::vector<put_target_t> *targets)
{
  if (targets == nullptr || locator_cache == nullptr ||
      bucket_name.empty() || index_name.empty()) {
    return -EINVAL;
  }
  targets->clear();

  const std::string vector_hash =
    vector_placement::hash_v0_vector_hash(vector_data);
  std::string sub_oid_name;
  if (vector_placement::pg_lsh_v0::sub_oid_enabled(
        config.distance_bucket_bits, config.residual_bits)) {
    vector_placement::pg_lsh_v0::sub_oid_t sub_oid;
    int ret = compute_sub_oid(vector_data, config, &sub_oid);
    if (ret < 0) {
      return ret;
    }
    sub_oid_name = vector_placement::pg_lsh_v0::format_sub_oid(
        sub_oid, sub_oid_config_view(config));
  }

  if (config.bucket_locators) {
    // The vector goes to its exact LSH bucket in each of the first d
    // tables. The bucket's PG is wherever RADOS maps its locator now.
    int ret = validate_index_config(config, pool_info);
    if (ret < 0) {
      return ret;
    }
    std::vector<vector_placement::pg_lsh_v0_group_t> exact_groups;
    ret = exact_lsh_groups(vector_data, config, &exact_groups);
    if (ret < 0) {
      return ret;
    }
    for (const auto& group : exact_groups) {
      if (targets->size() == config.d) {
        break;
      }
      const uint32_t ordinal =
        lsh_bucket_ordinal(group.table, group.lsh_bucket_id, config.k);
      std::string locator_key;
      uint32_t pg = 0;
      ret = locator_cache->bucket_locator(ordinal, &locator_key, &pg);
      if (ret < 0) {
        return ret;
      }
      targets->push_back({
        pg,
        vector_placement::make_pg_lsh_v0_oid(
            bucket_name, index_name, ordinal, sub_oid_name),
        std::move(locator_key),
        vector_placement::pg_lsh_v0_placement_key(ordinal),
        vector_hash,
        sub_oid_name,
        0,
      });
    }
    return 0;
  }

  std::vector<uint32_t> write_pgs;
  int ret = select_write_pgs(
      vector_data, config, pool_info, &write_pgs);
  if (ret < 0) {
    return ret;
  }

  targets->reserve(write_pgs.size());
  for (const uint32_t pg : write_pgs) {
    std::string locator_key;
    ret = locator_cache->locator_for_pg(pg, &locator_key);
    if (ret < 0) {
      return ret;
    }
    targets->push_back({
      pg,
      vector_placement::make_pg_lsh_v0_oid(
          bucket_name, index_name, pg, sub_oid_name),
      std::move(locator_key),
      vector_placement::pg_lsh_v0_placement_key(pg),
      vector_hash,
      sub_oid_name,
      config.partition_num,
    });
  }
  return 0;
}

// sub_oid selection for build_query_probes(). Split out because it depends
// only on (query_vector, config, query_params) and not on PG routing, so it
// can be tested without an ioctx. Under server_side_boundary_search it
// returns the exact sub_oid alone; validate_query_params() has already
// rejected a nonzero radius in that mode.
inline int select_probe_sub_oids(const ceph::bufferlist& query_vector,
                                 const index_config_t& config,
                                 const query_params_t& query_params,
                                 std::vector<std::string> *probe_sub_oids)
{
  if (probe_sub_oids == nullptr) {
    return -EINVAL;
  }
  probe_sub_oids->clear();

  if (!vector_placement::pg_lsh_v0::sub_oid_enabled(
        config.distance_bucket_bits, config.residual_bits)) {
    probe_sub_oids->emplace_back();
    return 0;
  }

  vector_placement::pg_lsh_v0::sub_oid_t exact_sub_oid;
  int ret = compute_sub_oid(query_vector, config, &exact_sub_oid);
  if (ret < 0) {
    return ret;
  }

  if (query_params.server_side_boundary_search) {
    probe_sub_oids->push_back(vector_placement::pg_lsh_v0::format_sub_oid(
        exact_sub_oid, sub_oid_config_view(config)));
    return 0;
  }

  const vector_placement::pg_lsh_v0::probe_config_t probe_config = {
    query_params.distance_bucket_radius,
    query_params.residual_hamming_radius,
  };
  return vector_placement::pg_lsh_v0::build_probe_sub_oids(
      exact_sub_oid, sub_oid_config_view(config), probe_config,
      probe_sub_oids);
}

// bucket_locators: plain hash routing at the current pg_num. The query's
// LSH groups (exact bucket first, then the multi-probe neighbours, in the
// usual order) are mapped to PGs by RADOS itself (bucket_locator(), i.e.
// ceph_stable_mod(hash_key(bucket key), pg_num, mask)); the first m
// distinct PGs are probed once each. Nothing depends on how the pool got
// to its pg_num. Each probe is anchored at the group's bucket and the
// query's exact sub_oid; a PG-band tree search then covers every bucket
// the PG holds.
inline int build_bucket_locator_probes(
    const std::string& bucket_name,
    const std::string& index_name,
    const ceph::bufferlist& query_vector,
    const index_config_t& config,
    const query_params_t& query_params,
    const std::vector<std::string>& probe_sub_oids,
    locator_cache_t *locator_cache,
    std::vector<query_probe_t> *probes,
    uint64_t *generated_group_count)
{
  if (config.bucket_locators == bucket_locators_lsh_aligned &&
      query_params.query_routing.table_count != 1) {
    // Only table 0 is aligned (see validate_index_config()); a table t > 0
    // bucket would land in the PG of the table-0 bucket with the same id.
    // Checked here rather than in validate_query_params(), which also runs
    // for load and census callers that carry no query routing.
    return -EINVAL;
  }
  std::vector<vector_placement::pg_lsh_v0_group_t> groups;
  int ret = query_lsh_groups(
      query_vector, config, query_params.hamming_radius,
      query_params.query_routing, &groups);
  if (ret < 0) {
    return ret;
  }
  if (generated_group_count != nullptr) {
    *generated_group_count = groups.size();
  }
  const std::string& sub_oid = probe_sub_oids.front();
  std::set<uint32_t> probed_pgs;
  for (const auto& group : groups) {
    if (probes->size() == query_params.m) {
      break;
    }
    const uint32_t ordinal =
      lsh_bucket_ordinal(group.table, group.lsh_bucket_id, config.k);
    std::string locator_key;
    uint32_t pg = 0;
    ret = locator_cache->bucket_locator(ordinal, &locator_key, &pg);
    if (ret < 0) {
      return ret;
    }
    if (!probed_pgs.insert(pg).second) {
      continue;
    }
    probes->push_back({
      pg,
      group.hamming_distance,
      1,
      vector_placement::make_pg_lsh_v0_oid(
          bucket_name, index_name, ordinal, sub_oid),
      std::move(locator_key),
      vector_placement::pg_lsh_v0_placement_key(ordinal),
      sub_oid,
      0,
    });
  }
  return 0;
}

inline int build_query_probes(const std::string& bucket_name,
                              const std::string& index_name,
                              const ceph::bufferlist& query_vector,
                              const index_config_t& config,
                              const query_params_t& query_params,
                              const pool_pg_info_t& pool_info,
                              locator_cache_t *locator_cache,
                              std::vector<query_probe_t> *probes,
                              uint64_t *generated_group_count)
{
  if (probes == nullptr || locator_cache == nullptr ||
      bucket_name.empty() || index_name.empty()) {
    return -EINVAL;
  }
  probes->clear();
  if (generated_group_count != nullptr) {
    *generated_group_count = 0;
  }

  int ret = validate_query_params(config, query_params, pool_info);
  if (ret < 0) {
    return ret;
  }

  std::vector<std::string> probe_sub_oids;
  ret = select_probe_sub_oids(
      query_vector, config, query_params, &probe_sub_oids);
  if (ret < 0) {
    return ret;
  }

  if (config.bucket_locators) {
    return build_bucket_locator_probes(
        bucket_name, index_name, query_vector, config, query_params,
        probe_sub_oids, locator_cache, probes, generated_group_count);
  }

  std::vector<vector_placement::pg_lsh_v0_ranked_pg_t> ranked;
  ret = select_query_pgs(
      query_vector, config, query_params, pool_info, &ranked,
      generated_group_count);
  if (ret < 0) {
    return ret;
  }

  // Routing ranks PGs at pool_info.pg_num. In partition mode each routed
  // PG j holds partitions j, j + pg_num, j + 2 * pg_num, ... (h % pg_num ==
  // (h % partition_num) % pg_num), so the probe covers all of them.
  const uint32_t partition_stride = pool_info.pg_num;
  const uint32_t partitions_per_pg = config.partition_num != 0
    ? config.partition_num / partition_stride : 1;
  probes->reserve(
      ranked.size() * partitions_per_pg * probe_sub_oids.size());
  std::unordered_set<std::string> seen_oid_names;
  for (const auto& ranked_pg : ranked) {
    for (uint32_t child = 0; child < partitions_per_pg; ++child) {
      const uint32_t partition = ranked_pg.pg + child * partition_stride;
      std::string locator_key;
      ret = locator_cache->locator_for_pg(partition, &locator_key);
      if (ret < 0) {
        return ret;
      }
      uint32_t emitted_for_pg = 0;
      for (const auto& probe_sub_oid : probe_sub_oids) {
        object_t oid = vector_placement::make_pg_lsh_v0_oid(
            bucket_name, index_name, partition, probe_sub_oid);
        if (!seen_oid_names.insert(oid.name).second) {
          continue;
        }
        probes->push_back({
          partition,
          ranked_pg.min_hamming_distance,
          ranked_pg.table_votes,
          std::move(oid),
          locator_key,
          vector_placement::pg_lsh_v0_placement_key(partition),
          probe_sub_oid,
          config.partition_num,
        });
        ++emitted_for_pg;
        if (query_params.probe_limit_per_pg != 0 &&
            emitted_for_pg >= query_params.probe_limit_per_pg) {
          break;
        }
      }
    }
  }
  return 0;
}

// Fills req.boundary_search so the OSD can recompute Dmin/Dmax and the
// residual wildcard mask itself. Left unset unless the query asked for
// server-side boundary search, so a plain probe carries no extra bytes.
inline void apply_boundary_search_config(
    const index_config_t& config,
    const query_params_t& query_params,
    ceph::rados::query_vectors_request_t *req)
{
  if (req == nullptr) {
    return;
  }
  if (!query_params.server_side_boundary_search) {
    req->boundary_search.reset();
    return;
  }
  ceph::rados::vector_boundary_search_config_t boundary;
  boundary.anchor = config.anchor;
  boundary.seed = config.seed;
  boundary.distance_bucket_bits = config.distance_bucket_bits;
  boundary.residual_bits = config.residual_bits;
  req->boundary_search = std::move(boundary);
}

// Same as apply_boundary_search_config() above, for the independent
// tree-traversal mechanism.
inline void apply_tree_boundary_search_config(
    const index_config_t& config,
    const query_params_t& query_params,
    ceph::rados::query_vectors_request_t *req)
{
  if (req == nullptr) {
    return;
  }
  if (query_params.tree_boundary_search_budget == 0) {
    req->tree_boundary_search.reset();
    return;
  }
  ceph::rados::vector_tree_boundary_search_config_t tree_boundary;
  tree_boundary.anchor = config.anchor;
  tree_boundary.seed = config.seed;
  tree_boundary.distance_bucket_bits = config.distance_bucket_bits;
  tree_boundary.residual_bits = config.residual_bits;
  tree_boundary.budget = query_params.tree_boundary_search_budget;
  tree_boundary.distance_geometry =
    config.placement_layout_version == pg_lsh_layout_version_raw_euclidean_v1
      ? ceph::rados::vector_pg_lsh_placement::distance_geometry_raw_euclidean_v1
      : ceph::rados::vector_pg_lsh_placement::distance_geometry_normalized_angular_v0;
  tree_boundary.raw_distance_scale_max = config.raw_distance_scale_max;
  tree_boundary.enable_pruning = query_params.tree_boundary_search_prune;
  tree_boundary.no_improve_patience =
      query_params.tree_boundary_search_no_improve_patience;
  tree_boundary.onode_budget =
      query_params.tree_boundary_search_onode_budget;
  if (config.bucket_locators && query_params.band_pg_num != 0) {
    // Search the whole probed PG (every bucket in it), not just the
    // anchor bucket's hash: the PG is the hash keys sharing its low
    // log2(pg_num) bits.
    uint32_t band_bits = 0;
    while ((1u << band_bits) < query_params.band_pg_num) {
      ++band_bits;
    }
    tree_boundary.band_bits = band_bits;
  }
  req->tree_boundary_search = std::move(tree_boundary);
}

inline int verify_probe_locator(v14_2_0::IoCtx& ioctx,
                                uint32_t pg,
                                uint32_t partition_num,
                                const std::string& locator_key)
{
  return check_locator(ioctx, pg, partition_num, locator_key);
}

CEPH_RADOS_API int put_vector(v14_2_0::IoCtx& ioctx,
                              const put_target_t& target,
                              ceph::rados::put_vector_request_t req);

CEPH_RADOS_API int submit_put(v14_2_0::IoCtx& ioctx,
                              const put_target_t& target,
                              ceph::rados::put_vector_request_t req,
                              put_op_state_t *op_state,
                              v14_2_0::AioCompletion *completion);

CEPH_RADOS_API int submit_query(v14_2_0::IoCtx& ioctx,
                                const query_probe_t& probe,
                                ceph::rados::query_vectors_request_t req,
                                query_op_state_t *op_state,
                                v14_2_0::AioCompletion *completion,
                                ceph::bufferlist *reply,
                                int *op_rval);

// One RADOS request carrying several QUERY_VECTORS ops against the same
// object. Each element of `reqs` becomes its own op in one
// ObjectOperation, so the OSD runs them back to back under a single
// MOSDOp/MOSDOpReply pair and a single obc lock, and each op's reply
// lands in the matching element of `replies`/`op_rvals`.
//
// Nothing about an individual op differs from submit_query() above: same
// opcode, same request/response encoding, same OSD handler. Only the
// number of ops per request changes, which is why every probe in a batch
// must resolve to the same object -- a RADOS op vector addresses exactly
// one object.
CEPH_RADOS_API int submit_query_multi(
    v14_2_0::IoCtx& ioctx,
    const query_probe_t& probe,
    const std::vector<ceph::rados::query_vectors_request_t>& reqs,
    query_op_state_t *op_state,
    v14_2_0::AioCompletion *completion,
    const std::vector<ceph::bufferlist*>& replies,
    const std::vector<int*>& op_rvals);

CEPH_RADOS_API int query_sync(v14_2_0::IoCtx& ioctx,
                              const query_probe_t& probe,
                              ceph::rados::query_vectors_request_t req,
                              ceph::bufferlist *reply,
                              int *op_rval);

} // namespace vector_pg_lsh
} // inline namespace v14_2_0
} // namespace librados

#endif
