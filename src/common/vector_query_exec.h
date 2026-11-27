// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_COMMON_VECTOR_QUERY_EXEC_H
#define CEPH_COMMON_VECTOR_QUERY_EXEC_H

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <limits>
#include <string>
#include <string_view>
#include <vector>

#include <errno.h>

#include "include/buffer.h"
#include "include/encoding.h"
#include "include/rados/vector_ops.h"
#include "common/vector_record.h"
#include "common/vector_query_exec_int8_simd.h"

#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
#include "arch/probe.h"
#include "arch/intel.h"
#define CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD 1
#endif

namespace ceph {
namespace rados {
namespace vector_query_exec {

// Non-owning view. The backing strings and vector data must remain valid
// until consume() returns.
struct vector_entry_view_t {
  std::string_view entry_id;
  std::string_view bucket_name;
  std::string_view index_name;
  std::string_view user_key;
  std::string_view placement_key;
  uint32_t data_type = 0;
  uint32_t distance_metric = 0;
  uint32_t dimension = 0;
  bool has_data_type = false;
  bool has_distance_metric = false;
  bool has_dimension = false;
  // True when metadata references vector content. OMAP may still be missing
  // that payload; VectorNode records always provide it inline.
  bool has_vector_reference = false;
  // True for a row scanned directly out of a VectorNode LIST page: LIST
  // rows carry only entry_id and vector bytes (see vector_node_layout.h's
  // vector_list_entry_le_t), so bucket_name/index_name/user_key/
  // placement_key are never populated for this source. That is safe to
  // match on anyway: object-name placement already scopes one object to
  // one (bucket_name, index_name) pair (see
  // librados/vector_placement.h's make_*_oid()), and the caller already
  // scoped the scanned LIST chain to this exact (dimension, data_type) via
  // find_group_head() before ever visiting a row, so re-checking those
  // fields per row would be redundant. user_key (and any application
  // metadata) is restored afterwards, by entry_id, from OMAP -- but only
  // for the entries that survive top-k selection, not every scanned row.
  bool is_vector_node_native = false;
  // Fixed-layout VectorNode records expose a contiguous, non-owning range.
  // It is valid only while the pinned LIST extent remains alive.
  std::string_view contiguous_vector_data;
  // OMAP content can remain fragmented and continues to use bufferlist.
  // At most one of the two payload sources may be populated.
  const ceph::bufferlist* vector_data = nullptr;

  bool has_vector_payload() const {
    return !contiguous_vector_data.empty() || vector_data != nullptr;
  }

  bool has_ambiguous_vector_payload() const {
    return !contiguous_vector_data.empty() && vector_data != nullptr;
  }
};

struct query_filter_stats_t {
  uint64_t total_entries = 0;
  uint64_t incomplete_entries = 0;
  uint64_t bucket_mismatch = 0;
  uint64_t index_mismatch = 0;
  uint64_t data_type_mismatch = 0;
  uint64_t distance_metric_mismatch = 0;
  uint64_t dimension_mismatch = 0;
  uint64_t probe_mismatch = 0;
  uint64_t missing_content = 0;
  uint64_t distance_error = 0;
  uint64_t matched_entries = 0;
  uint64_t distance_computations = 0;
  uint64_t merged_entries = 0;
  uint64_t final_entries = 0;
};

inline bool starts_with(std::string_view value, std::string_view prefix)
{
  return value.size() >= prefix.size() &&
    value.substr(0, prefix.size()) == prefix;
}

inline bool is_lower_hex(char c)
{
  return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
}

inline bool is_lower_hex_string(std::string_view value, size_t len)
{
  if (value.size() != len) {
    return false;
  }
  for (char c : value) {
    if (!is_lower_hex(c)) {
      return false;
    }
  }
  return true;
}

inline bool is_hash_v0_placement_key(std::string_view placement_key)
{
  return is_lower_hex_string(
      placement_key, vector_hash_v0_placement_key_len);
}

inline bool is_hash_v0_vector_hash(std::string_view vector_hash)
{
  return is_lower_hex_string(vector_hash, vector_hash_v0_vector_hash_len);
}

inline bool is_lsh_v0_placement_key(std::string_view placement_key)
{
  return is_lower_hex_string(
      placement_key, vector_lsh_v0_placement_key_len);
}

inline bool is_pg_lsh_v0_placement_key(std::string_view placement_key)
{
  return is_hash_v0_vector_hash(placement_key);
}

inline bool is_supported_placement_key(std::string_view placement_algorithm,
                                       std::string_view placement_key)
{
  if (placement_algorithm == vector_placement_algorithm_hash_v0) {
    return is_hash_v0_placement_key(placement_key);
  }
  if (placement_algorithm == vector_placement_algorithm_lsh_v0) {
    return is_lsh_v0_placement_key(placement_key);
  }
  if (placement_algorithm == vector_placement_algorithm_pg_lsh_v0) {
    return is_pg_lsh_v0_placement_key(placement_key);
  }
  return false;
}

inline bool is_supported_probe_key(std::string_view placement_key)
{
  return is_hash_v0_placement_key(placement_key) ||
    is_lsh_v0_placement_key(placement_key);
}

inline int validate_vector_payload(uint32_t data_type,
                                   uint32_t distance_metric,
                                   uint32_t dimension,
                                   const ceph::bufferlist& vector_data,
                                   size_t *element_size = nullptr)
{
  if (dimension == 0 || dimension > vector_max_dimension) {
    return -EINVAL;
  }

  size_t type_size = 0;
  int r = vector_data_type_size(data_type, &type_size);
  if (r < 0) {
    return -EINVAL;
  }
  if (!vector_distance_metric_supported(distance_metric)) {
    return -EINVAL;
  }

  const size_t expected_len = static_cast<size_t>(dimension) * type_size;
  if (vector_data.length() != expected_len) {
    return -EINVAL;
  }

  if (element_size != nullptr) {
    *element_size = type_size;
  }
  return 0;
}

inline int validate_put_request(const put_vector_request_t& req,
                                bool require_routed_fields,
                                size_t *element_size = nullptr)
{
  if (req.bucket_name.empty() || req.index_name.empty() || req.key.empty()) {
    return -EINVAL;
  }

  int r = validate_vector_payload(req.data_type, req.distance_metric,
                                  req.dimension, req.vector_data,
                                  element_size);
  if (r < 0) {
    return r;
  }

  if (!require_routed_fields) {
    return 0;
  }

  if (!is_supported_placement_key(req.placement_algorithm,
                                  req.placement_key) ||
      !is_hash_v0_vector_hash(req.vector_hash)) {
    return -EINVAL;
  }
  return 0;
}

template <typename T>
inline bool decode_omap_value(const ceph::bufferlist& bl, T *out)
{
  try {
    T decoded;
    auto p = bl.cbegin();
    decode(decoded, p);
    if (!p.end()) {
      return false;
    }
    *out = decoded;
    return true;
  } catch (const ceph::buffer::error&) {
    return false;
  }
}

inline void decode_omap_string_value(const ceph::bufferlist& bl,
                                     std::string *out)
{
  if (!decode_omap_value(bl, out)) {
    out->clear();
  }
}

inline int validate_query_request(const query_vectors_request_t& req,
                                  size_t *element_size = nullptr)
{
  if (req.bucket_name.empty() || req.index_name.empty() ||
      req.local_top_k == 0) {
    return -EINVAL;
  }

  int r = validate_vector_payload(req.data_type, req.distance_metric,
                                  req.dimension, req.query_vector,
                                  element_size);
  if (r < 0) {
    return r;
  }

  for (const auto& prefix : req.probe_prefixes) {
    if (!is_supported_probe_key(prefix)) {
      return -EINVAL;
    }
  }
  return 0;
}

inline bool probe_matches(const query_vectors_request_t& req,
                          const vector_entry_view_t& entry)
{
  if (req.probe_prefixes.empty()) {
    return true;
  }
  if (entry.placement_key.empty()) {
    return false;
  }
  return std::find(req.probe_prefixes.begin(), req.probe_prefixes.end(),
                   entry.placement_key) != req.probe_prefixes.end();
}

inline float read_float32(const char *data, size_t index)
{
  float value = 0;
  std::memcpy(&value, data + index * sizeof(float), sizeof(float));
  return value;
}

// int8 candidates are read as the small integers they are: the value is
// exactly representable in float, so every int8 kernel below performs the
// same arithmetic as its float32 twin on the same numbers.
inline float read_int8(const char *data, size_t index)
{
  int8_t value = 0;
  std::memcpy(&value, data + index, sizeof(value));
  return static_cast<float>(value);
}

// Running sums for one query/candidate pair, shared by every kernel.
struct distance_terms_t {
  double dot = 0;
  double candidate_norm_squared = 0;
  double sum_sq = 0;
};

inline void accumulate_distance_terms_scalar(const float *query,
                                             const char *candidate_bytes,
                                             uint32_t dimension,
                                             distance_terms_t *terms)
{
  for (uint32_t i = 0; i < dimension; ++i) {
    const double q = query[i];
    const double c = read_float32(candidate_bytes, i);
    terms->dot += q * c;
    terms->candidate_norm_squared += c * c;
    const double diff = q - c;
    terms->sum_sq += diff * diff;
  }
}

// Same accumulation over an int8 candidate row. The query stays float --
// vector_data_type_int8 quantizes it too, so these floats hold the query's
// integer codes and both sides are in the same quantized units.
inline void accumulate_distance_terms_int8_scalar(const float *query,
                                                  const char *candidate_bytes,
                                                  uint32_t dimension,
                                                  distance_terms_t *terms)
{
  for (uint32_t i = 0; i < dimension; ++i) {
    const double q = query[i];
    const double c = read_int8(candidate_bytes, i);
    terms->dot += q * c;
    terms->candidate_norm_squared += c * c;
    const double diff = q - c;
    terms->sum_sq += diff * diff;
  }
}

#if defined(CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD)
// Converts each 4-wide float32 group to double before accumulating, using
// a separate multiply and add instead of fused multiply-add, so
// per-element arithmetic matches accumulate_distance_terms_scalar. The
// cross-lane reduction still sums in a different order than the scalar
// loop; the resulting difference is far below float32 precision.
__attribute__((__target__("avx2")))
inline void accumulate_distance_terms_avx2(const float *query,
                                           const char *candidate_bytes,
                                           uint32_t dimension,
                                           distance_terms_t *terms)
{
  __m256d dot_acc = _mm256_setzero_pd();
  __m256d norm_acc = _mm256_setzero_pd();
  __m256d sq_acc = _mm256_setzero_pd();

  uint32_t i = 0;
  for (; i + 4 <= dimension; i += 4) {
    const __m256d q = _mm256_cvtps_pd(_mm_loadu_ps(query + i));
    const __m256d c = _mm256_cvtps_pd(_mm_loadu_ps(
        reinterpret_cast<const float*>(candidate_bytes + i * sizeof(float))));
    dot_acc = _mm256_add_pd(dot_acc, _mm256_mul_pd(q, c));
    norm_acc = _mm256_add_pd(norm_acc, _mm256_mul_pd(c, c));
    const __m256d diff = _mm256_sub_pd(q, c);
    sq_acc = _mm256_add_pd(sq_acc, _mm256_mul_pd(diff, diff));
  }

  alignas(32) double dot_lanes[4];
  alignas(32) double norm_lanes[4];
  alignas(32) double sq_lanes[4];
  _mm256_store_pd(dot_lanes, dot_acc);
  _mm256_store_pd(norm_lanes, norm_acc);
  _mm256_store_pd(sq_lanes, sq_acc);
  for (int lane = 0; lane < 4; ++lane) {
    terms->dot += dot_lanes[lane];
    terms->candidate_norm_squared += norm_lanes[lane];
    terms->sum_sq += sq_lanes[lane];
  }

  for (; i < dimension; ++i) {
    const double q = query[i];
    const double c = read_float32(candidate_bytes, i);
    terms->dot += q * c;
    terms->candidate_norm_squared += c * c;
    const double diff = q - c;
    terms->sum_sq += diff * diff;
  }
}

// int8 twin of accumulate_distance_terms_avx2(): four candidate bytes are
// sign-extended to int32 and widened to double, so the lane order and the
// arithmetic match the float32 kernel exactly -- only the load differs,
// and it moves a quarter of the bytes.
__attribute__((__target__("avx2")))
inline void accumulate_distance_terms_int8_avx2(const float *query,
                                                const char *candidate_bytes,
                                                uint32_t dimension,
                                                distance_terms_t *terms)
{
  __m256d dot_acc = _mm256_setzero_pd();
  __m256d norm_acc = _mm256_setzero_pd();
  __m256d sq_acc = _mm256_setzero_pd();

  uint32_t i = 0;
  for (; i + 4 <= dimension; i += 4) {
    const __m256d q = _mm256_cvtps_pd(_mm_loadu_ps(query + i));
    int32_t packed = 0;
    std::memcpy(&packed, candidate_bytes + i, sizeof(packed));
    const __m256d c = _mm256_cvtepi32_pd(
        _mm_cvtepi8_epi32(_mm_cvtsi32_si128(packed)));
    dot_acc = _mm256_add_pd(dot_acc, _mm256_mul_pd(q, c));
    norm_acc = _mm256_add_pd(norm_acc, _mm256_mul_pd(c, c));
    const __m256d diff = _mm256_sub_pd(q, c);
    sq_acc = _mm256_add_pd(sq_acc, _mm256_mul_pd(diff, diff));
  }

  alignas(32) double dot_lanes[4];
  alignas(32) double norm_lanes[4];
  alignas(32) double sq_lanes[4];
  _mm256_store_pd(dot_lanes, dot_acc);
  _mm256_store_pd(norm_lanes, norm_acc);
  _mm256_store_pd(sq_lanes, sq_acc);
  for (int lane = 0; lane < 4; ++lane) {
    terms->dot += dot_lanes[lane];
    terms->candidate_norm_squared += norm_lanes[lane];
    terms->sum_sq += sq_lanes[lane];
  }

  for (; i < dimension; ++i) {
    const double q = query[i];
    const double c = read_int8(candidate_bytes, i);
    terms->dot += q * c;
    terms->candidate_norm_squared += c * c;
    const double diff = q - c;
    terms->sum_sq += diff * diff;
  }
}
#endif // CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD

inline void accumulate_distance_terms(const float *query,
                                      const char *candidate_bytes,
                                      uint32_t dimension,
                                      distance_terms_t *terms)
{
#if defined(CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD)
  static const bool has_avx2 = [] {
    ceph_arch_probe();
    return ceph_arch_intel_avx2 != 0;
  }();
  if (has_avx2) {
    accumulate_distance_terms_avx2(query, candidate_bytes, dimension, terms);
    return;
  }
#endif
  accumulate_distance_terms_scalar(query, candidate_bytes, dimension, terms);
}

inline void accumulate_distance_terms_int8(const float *query,
                                           const char *candidate_bytes,
                                           uint32_t dimension,
                                           distance_terms_t *terms)
{
#if defined(CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD)
  static const bool has_avx2 = [] {
    ceph_arch_probe();
    return ceph_arch_intel_avx2 != 0;
  }();
  if (has_avx2) {
    accumulate_distance_terms_int8_avx2(
        query, candidate_bytes, dimension, terms);
    return;
  }
#endif
  accumulate_distance_terms_int8_scalar(
      query, candidate_bytes, dimension, terms);
}

// Single entry point for the scan paths: picks the kernel from the row's
// data_type. Unknown types are rejected before any candidate is read.
inline int accumulate_distance_terms_typed(uint32_t data_type,
                                           const float *query,
                                           const char *candidate_bytes,
                                           uint32_t dimension,
                                           distance_terms_t *terms)
{
  switch (data_type) {
  case vector_data_type_float32:
    accumulate_distance_terms(query, candidate_bytes, dimension, terms);
    return 0;
  case vector_data_type_int8:
    accumulate_distance_terms_int8(query, candidate_bytes, dimension, terms);
    return 0;
  default:
    return -EINVAL;
  }
}

inline int copy_float32_values(const ceph::bufferlist& data,
                               uint32_t dimension,
                               std::vector<float> *out_values)
{
  if (out_values == nullptr || dimension == 0) {
    return -EINVAL;
  }
  const size_t expected_len =
    static_cast<size_t>(dimension) * sizeof(float);
  if (data.length() != expected_len) {
    return -EINVAL;
  }

  out_values->resize(dimension);
  auto p = data.cbegin();
  p.copy(expected_len, reinterpret_cast<char*>(out_values->data()));
  return 0;
}

inline int copy_int8_values(const ceph::bufferlist& data,
                            uint32_t dimension,
                            std::vector<float> *out_values)
{
  if (out_values == nullptr || dimension == 0) {
    return -EINVAL;
  }
  if (data.length() != static_cast<size_t>(dimension)) {
    return -EINVAL;
  }
  ceph::bufferlist copy = data;
  const char *bytes = copy.c_str();
  out_values->resize(dimension);
  for (uint32_t i = 0; i < dimension; ++i) {
    (*out_values)[i] = read_int8(bytes, i);
  }
  return 0;
}

// A query is carried in the same data_type as the rows it is compared
// against, and is always decoded into floats: for int8 those floats hold
// the query's integer codes, so every kernel keeps one query
// representation regardless of how the rows are stored.
inline int copy_query_values(uint32_t data_type,
                             const ceph::bufferlist& data,
                             uint32_t dimension,
                             std::vector<float> *out_values)
{
  switch (data_type) {
  case vector_data_type_float32:
    return copy_float32_values(data, dimension, out_values);
  case vector_data_type_int8:
    return copy_int8_values(data, dimension, out_values);
  default:
    return -EINVAL;
  }
}

inline double squared_l2_norm(const std::vector<float>& values)
{
  double norm_squared = 0;
  for (const float value : values) {
    norm_squared += static_cast<double>(value) * value;
  }
  return norm_squared;
}

// ---- batched kernel -------------------------------------------------
//
// One stored vector against many queries at once. accumulate_distance_terms()
// above recomputes candidate_norm_squared for every (query, candidate) pair,
// but that term depends only on the candidate; and every metric here can be
// written from the triple (||q||^2, ||v||^2, q.v):
//
//   euclidean : sqrt(||q||^2 + ||v||^2 - 2 q.v)
//   cosine    : 1 - q.v / (||q|| ||v||)
//   dot       : -q.v
//
// so a batch pays for ||v||^2 once per record and for a plain dot product
// per query. The queries are one row-major matrix and four rows are driven
// against the same candidate registers at a time, so the candidate is
// loaded once per four queries rather than once per query.
//
// Lane order matches accumulate_distance_terms_*, so `dot` and
// candidate_norm_squared come out bit-identical to the single-query kernel
// -- and therefore so do the cosine and dot metrics. Euclidean does NOT:
// deriving ||q-v||^2 from the three terms sums differently than
// accumulating (q-v)^2 directly. Both run in double and land in float32.

inline double candidate_norm_squared_scalar(const char *candidate_bytes,
                                            uint32_t dimension)
{
  double norm_squared = 0;
  for (uint32_t i = 0; i < dimension; ++i) {
    const double c = read_float32(candidate_bytes, i);
    norm_squared += c * c;
  }
  return norm_squared;
}

inline void accumulate_dots_scalar(const float *queries,
                                   size_t stride,
                                   size_t query_count,
                                   const char *candidate_bytes,
                                   uint32_t dimension,
                                   double *dots)
{
  for (size_t q = 0; q < query_count; ++q) {
    const float *query = queries + q * stride;
    double dot = 0;
    for (uint32_t i = 0; i < dimension; ++i) {
      dot += static_cast<double>(query[i]) * read_float32(candidate_bytes, i);
    }
    dots[q] = dot;
  }
}

inline double candidate_norm_squared_int8_scalar(const char *candidate_bytes,
                                            uint32_t dimension)
{
  double norm_squared = 0;
  for (uint32_t i = 0; i < dimension; ++i) {
    const double c = read_int8(candidate_bytes, i);
    norm_squared += c * c;
  }
  return norm_squared;
}

inline void accumulate_dots_int8_scalar(const float *queries,
                                   size_t stride,
                                   size_t query_count,
                                   const char *candidate_bytes,
                                   uint32_t dimension,
                                   double *dots)
{
  for (size_t q = 0; q < query_count; ++q) {
    const float *query = queries + q * stride;
    double dot = 0;
    for (uint32_t i = 0; i < dimension; ++i) {
      dot += static_cast<double>(query[i]) * read_int8(candidate_bytes, i);
    }
    dots[q] = dot;
  }
}

#if defined(CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD)
__attribute__((__target__("avx2")))
inline double candidate_norm_squared_avx2(const char *candidate_bytes,
                                          uint32_t dimension)
{
  __m256d norm_acc = _mm256_setzero_pd();
  uint32_t i = 0;
  for (; i + 4 <= dimension; i += 4) {
    const __m256d c = _mm256_cvtps_pd(_mm_loadu_ps(
        reinterpret_cast<const float*>(candidate_bytes + i * sizeof(float))));
    norm_acc = _mm256_add_pd(norm_acc, _mm256_mul_pd(c, c));
  }
  alignas(32) double norm_lanes[4];
  _mm256_store_pd(norm_lanes, norm_acc);
  double norm_squared = 0;
  for (int lane = 0; lane < 4; ++lane) {
    norm_squared += norm_lanes[lane];
  }
  for (; i < dimension; ++i) {
    const double c = read_float32(candidate_bytes, i);
    norm_squared += c * c;
  }
  return norm_squared;
}

__attribute__((__target__("avx2")))
inline double horizontal_sum_pd(__m256d acc)
{
  alignas(32) double lanes[4];
  _mm256_store_pd(lanes, acc);
  return lanes[0] + lanes[1] + lanes[2] + lanes[3];
}

__attribute__((__target__("avx2")))
inline void accumulate_dots_avx2(const float *queries,
                                 size_t stride,
                                 size_t query_count,
                                 const char *candidate_bytes,
                                 uint32_t dimension,
                                 double *dots)
{
  size_t q = 0;
  // Four query rows per pass: one candidate load feeds all four.
  for (; q + 4 <= query_count; q += 4) {
    const float *q0 = queries + (q + 0) * stride;
    const float *q1 = queries + (q + 1) * stride;
    const float *q2 = queries + (q + 2) * stride;
    const float *q3 = queries + (q + 3) * stride;
    __m256d a0 = _mm256_setzero_pd();
    __m256d a1 = _mm256_setzero_pd();
    __m256d a2 = _mm256_setzero_pd();
    __m256d a3 = _mm256_setzero_pd();
    uint32_t i = 0;
    for (; i + 4 <= dimension; i += 4) {
      const __m256d c = _mm256_cvtps_pd(_mm_loadu_ps(
          reinterpret_cast<const float*>(
            candidate_bytes + i * sizeof(float))));
      a0 = _mm256_add_pd(a0, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(q0 + i)), c));
      a1 = _mm256_add_pd(a1, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(q1 + i)), c));
      a2 = _mm256_add_pd(a2, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(q2 + i)), c));
      a3 = _mm256_add_pd(a3, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(q3 + i)), c));
    }
    dots[q + 0] = horizontal_sum_pd(a0);
    dots[q + 1] = horizontal_sum_pd(a1);
    dots[q + 2] = horizontal_sum_pd(a2);
    dots[q + 3] = horizontal_sum_pd(a3);
    for (; i < dimension; ++i) {
      const double c = read_float32(candidate_bytes, i);
      dots[q + 0] += static_cast<double>(q0[i]) * c;
      dots[q + 1] += static_cast<double>(q1[i]) * c;
      dots[q + 2] += static_cast<double>(q2[i]) * c;
      dots[q + 3] += static_cast<double>(q3[i]) * c;
    }
  }
  for (; q < query_count; ++q) {
    const float *query = queries + q * stride;
    __m256d acc = _mm256_setzero_pd();
    uint32_t i = 0;
    for (; i + 4 <= dimension; i += 4) {
      const __m256d c = _mm256_cvtps_pd(_mm_loadu_ps(
          reinterpret_cast<const float*>(
            candidate_bytes + i * sizeof(float))));
      acc = _mm256_add_pd(acc, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(query + i)), c));
    }
    dots[q] = horizontal_sum_pd(acc);
    for (; i < dimension; ++i) {
      dots[q] += static_cast<double>(query[i]) *
                 read_float32(candidate_bytes, i);
    }
  }
}


// Four int8 candidate values, sign-extended and widened to double -- the
// only thing an int8 batch kernel does differently from its float32 twin.
__attribute__((__target__("avx2")))
inline __m256d load_int8x4_pd(const char *candidate_bytes, uint32_t i)
{
  int32_t packed = 0;
  std::memcpy(&packed, candidate_bytes + i, sizeof(packed));
  return _mm256_cvtepi32_pd(_mm_cvtepi8_epi32(_mm_cvtsi32_si128(packed)));
}

__attribute__((__target__("avx2")))
inline double candidate_norm_squared_int8_avx2(const char *candidate_bytes,
                                          uint32_t dimension)
{
  __m256d norm_acc = _mm256_setzero_pd();
  uint32_t i = 0;
  for (; i + 4 <= dimension; i += 4) {
    const __m256d c = load_int8x4_pd(candidate_bytes, i);
    norm_acc = _mm256_add_pd(norm_acc, _mm256_mul_pd(c, c));
  }
  alignas(32) double norm_lanes[4];
  _mm256_store_pd(norm_lanes, norm_acc);
  double norm_squared = 0;
  for (int lane = 0; lane < 4; ++lane) {
    norm_squared += norm_lanes[lane];
  }
  for (; i < dimension; ++i) {
    const double c = read_int8(candidate_bytes, i);
    norm_squared += c * c;
  }
  return norm_squared;
}

__attribute__((__target__("avx2")))
inline void accumulate_dots_int8_avx2(const float *queries,
                                 size_t stride,
                                 size_t query_count,
                                 const char *candidate_bytes,
                                 uint32_t dimension,
                                 double *dots)
{
  size_t q = 0;
  // Four query rows per pass: one candidate load feeds all four.
  for (; q + 4 <= query_count; q += 4) {
    const float *q0 = queries + (q + 0) * stride;
    const float *q1 = queries + (q + 1) * stride;
    const float *q2 = queries + (q + 2) * stride;
    const float *q3 = queries + (q + 3) * stride;
    __m256d a0 = _mm256_setzero_pd();
    __m256d a1 = _mm256_setzero_pd();
    __m256d a2 = _mm256_setzero_pd();
    __m256d a3 = _mm256_setzero_pd();
    uint32_t i = 0;
    for (; i + 4 <= dimension; i += 4) {
      const __m256d c = load_int8x4_pd(candidate_bytes, i);
      a0 = _mm256_add_pd(a0, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(q0 + i)), c));
      a1 = _mm256_add_pd(a1, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(q1 + i)), c));
      a2 = _mm256_add_pd(a2, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(q2 + i)), c));
      a3 = _mm256_add_pd(a3, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(q3 + i)), c));
    }
    dots[q + 0] = horizontal_sum_pd(a0);
    dots[q + 1] = horizontal_sum_pd(a1);
    dots[q + 2] = horizontal_sum_pd(a2);
    dots[q + 3] = horizontal_sum_pd(a3);
    for (; i < dimension; ++i) {
      const double c = read_int8(candidate_bytes, i);
      dots[q + 0] += static_cast<double>(q0[i]) * c;
      dots[q + 1] += static_cast<double>(q1[i]) * c;
      dots[q + 2] += static_cast<double>(q2[i]) * c;
      dots[q + 3] += static_cast<double>(q3[i]) * c;
    }
  }
  for (; q < query_count; ++q) {
    const float *query = queries + q * stride;
    __m256d acc = _mm256_setzero_pd();
    uint32_t i = 0;
    for (; i + 4 <= dimension; i += 4) {
      const __m256d c = load_int8x4_pd(candidate_bytes, i);
      acc = _mm256_add_pd(acc, _mm256_mul_pd(
          _mm256_cvtps_pd(_mm_loadu_ps(query + i)), c));
    }
    dots[q] = horizontal_sum_pd(acc);
    for (; i < dimension; ++i) {
      dots[q] += static_cast<double>(query[i]) *
                 read_int8(candidate_bytes, i);
    }
  }
}
#endif // CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD

inline bool vector_query_exec_has_avx2()
{
#if defined(CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD)
  static const bool has_avx2 = [] {
    ceph_arch_probe();
    return ceph_arch_intel_avx2 != 0;
  }();
  return has_avx2;
#else
  return false;
#endif
}

inline double candidate_norm_squared(const char *candidate_bytes,
                                     uint32_t dimension)
{
#if defined(CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD)
  if (vector_query_exec_has_avx2()) {
    return candidate_norm_squared_avx2(candidate_bytes, dimension);
  }
#endif
  return candidate_norm_squared_scalar(candidate_bytes, dimension);
}

// `queries` is row-major with `stride` floats between rows (stride ==
// dimension for a packed matrix). `dots` must hold `query_count` doubles.
inline void accumulate_dots(const float *queries,
                            size_t stride,
                            size_t query_count,
                            const char *candidate_bytes,
                            uint32_t dimension,
                            double *dots)
{
#if defined(CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD)
  if (vector_query_exec_has_avx2()) {
    accumulate_dots_avx2(queries, stride, query_count, candidate_bytes,
                         dimension, dots);
    return;
  }
#endif
  accumulate_dots_scalar(queries, stride, query_count, candidate_bytes,
                         dimension, dots);
}

inline double candidate_norm_squared_int8(const char *candidate_bytes,
                                          uint32_t dimension)
{
#if defined(CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD)
  if (vector_query_exec_has_avx2()) {
    return candidate_norm_squared_int8_avx2(candidate_bytes, dimension);
  }
#endif
  return candidate_norm_squared_int8_scalar(candidate_bytes, dimension);
}

inline void accumulate_dots_int8(const float *queries,
                                 size_t stride,
                                 size_t query_count,
                                 const char *candidate_bytes,
                                 uint32_t dimension,
                                 double *dots)
{
#if defined(CEPH_VECTOR_QUERY_EXEC_HAVE_X86_SIMD)
  if (vector_query_exec_has_avx2()) {
    accumulate_dots_int8_avx2(queries, stride, query_count, candidate_bytes,
                              dimension, dots);
    return;
  }
#endif
  accumulate_dots_int8_scalar(queries, stride, query_count, candidate_bytes,
                              dimension, dots);
}

// data_type-keyed entry points for the batched path. Callers validate the
// type once per batch, so these never see anything but the two known ones.
inline double candidate_norm_squared_typed(uint32_t data_type,
                                           const char *candidate_bytes,
                                           uint32_t dimension)
{
  return data_type == vector_data_type_int8
    ? candidate_norm_squared_int8(candidate_bytes, dimension)
    : candidate_norm_squared(candidate_bytes, dimension);
}

inline void accumulate_dots_typed(uint32_t data_type,
                                  const float *queries,
                                  size_t stride,
                                  size_t query_count,
                                  const char *candidate_bytes,
                                  uint32_t dimension,
                                  double *dots)
{
  if (data_type == vector_data_type_int8) {
    accumulate_dots_int8(queries, stride, query_count, candidate_bytes,
                         dimension, dots);
    return;
  }
  accumulate_dots(queries, stride, query_count, candidate_bytes,
                  dimension, dots);
}

// ---- int8 integer fast path (opt-in, reference-preserving) ----------
//
// The kernels above stay exactly as they were: they are the reference
// implementation, and they are what runs unless an integer kernel is both
// selected and usable. int8_simd::selected_kernel() decides, once, from
// CEPH_VECTOR_INT8_KERNEL and CPU feature detection.
//
// The float32 kernels are not reachable from here at all.

// Prepared once per batch; `active()` is false whenever the reference
// path must be used -- wrong data_type, no fast kernel selected, or a
// query whose floats are not exact int8 codes (see query_codes_t::build).
class int8_fast_path_t {
public:
  bool prepare(uint32_t data_type,
               const float *rows,
               size_t stride,
               size_t row_count,
               uint32_t dimension)
  {
    codes_.reset();
    if (data_type != vector_data_type_int8) {
      return false;
    }
    const auto kernel = int8_simd::selected_kernel();
    if (kernel == int8_simd::kernel_t::reference) {
      return false;
    }
    return codes_.build(kernel, rows, stride, row_count, dimension);
  }

  bool active() const { return codes_.valid(); }
  int8_simd::kernel_t kernel() const { return codes_.kernel(); }

  // One candidate against every prepared query row. Returns ||v||^2 and
  // fills `dots`, so the caller drives distance_from_terms() exactly as
  // it does for the reference batched kernel. Every value here is an
  // exact integer, so widening to double is lossless and the distances
  // come out bit-identical to the reference kernels'.
  double compute_dots(const char *candidate_bytes,
                      uint32_t dimension,
                      size_t row_count,
                      double *dots) const
  {
    const auto stats =
      int8_simd::candidate_stats(codes_.kernel(), candidate_bytes, dimension);
    int64_t *scratch = scratch_dots(row_count);
    int8_simd::dots(codes_, row_count, candidate_bytes, dimension, stats,
                    scratch);
    for (size_t i = 0; i < row_count; ++i) {
      dots[i] = static_cast<double>(scratch[i]);
    }
    return static_cast<double>(stats.norm_squared);
  }

  // Single-query form: the same triple the reference
  // accumulate_distance_terms_int8() would have accumulated.
  void compute_terms(const char *candidate_bytes,
                     uint32_t dimension,
                     distance_terms_t *terms) const
  {
    const auto exact =
      int8_simd::single_terms(codes_, candidate_bytes, dimension);
    terms->dot = static_cast<double>(exact.dot);
    terms->candidate_norm_squared =
      static_cast<double>(exact.candidate_norm_squared);
    terms->sum_sq = static_cast<double>(exact.sum_sq);
  }

private:
  int64_t *scratch_dots(size_t row_count) const
  {
    if (scratch_.size() < row_count) {
      scratch_.resize(row_count);
    }
    return scratch_.data();
  }

  int8_simd::query_codes_t codes_;
  mutable std::vector<int64_t> scratch_;
};

// Turns one (||q||^2, ||v||^2, q.v) triple into the request's metric.
inline int distance_from_terms(uint32_t distance_metric,
                               double query_norm_squared,
                               double candidate_norm_squared_value,
                               double dot,
                               float *distance)
{
  if (distance == nullptr) {
    return -EINVAL;
  }
  switch (distance_metric) {
  case vector_distance_metric_euclidean: {
    const double sum_sq =
      query_norm_squared + candidate_norm_squared_value - 2.0 * dot;
    // The three terms are each O(||v||^2) while their combination can be
    // near zero for a query that is (almost) one of the stored vectors,
    // so rounding can push it slightly negative. There is no negative
    // distance to report; clamp instead of handing sqrt a negative.
    *distance = static_cast<float>(std::sqrt(sum_sq > 0 ? sum_sq : 0.0));
    return 0;
  }
  case vector_distance_metric_cosine:
    if (query_norm_squared == 0 || candidate_norm_squared_value == 0) {
      *distance = std::numeric_limits<float>::infinity();
    } else {
      *distance = static_cast<float>(
          1.0 - dot /
          (std::sqrt(query_norm_squared) *
           std::sqrt(candidate_norm_squared_value)));
    }
    return 0;
  case vector_distance_metric_dot:
    *distance = static_cast<float>(-dot);
    return 0;
  default:
    return -EINVAL;
  }
}

inline int compute_float32_distance(const query_vectors_request_t& req,
                                    const std::vector<float>& query_values,
                                    double query_norm_squared,
                                    std::string_view candidate,
                                    float *distance)
{
  if (distance == nullptr) {
    return -EINVAL;
  }
  const size_t expected_len =
    static_cast<size_t>(req.dimension) * sizeof(float);
  if (req.data_type != vector_data_type_float32 ||
      query_values.size() != req.dimension ||
      candidate.size() != expected_len) {
    return -EINVAL;
  }

  distance_terms_t terms;
  accumulate_distance_terms(
      query_values.data(), candidate.data(), req.dimension, &terms);

  switch (req.distance_metric) {
  case vector_distance_metric_euclidean:
    *distance = static_cast<float>(std::sqrt(terms.sum_sq));
    return 0;
  case vector_distance_metric_cosine:
    if (query_norm_squared == 0 || terms.candidate_norm_squared == 0) {
      *distance = std::numeric_limits<float>::infinity();
    } else {
      *distance = static_cast<float>(
          1.0 - terms.dot /
          (std::sqrt(query_norm_squared) *
           std::sqrt(terms.candidate_norm_squared)));
    }
    return 0;
  case vector_distance_metric_dot:
    *distance = static_cast<float>(-terms.dot);
    return 0;
  default:
    return -EINVAL;
  }
}

inline int compute_float32_distance(const query_vectors_request_t& req,
                                    const std::vector<float>& query_values,
                                    double query_norm_squared,
                                    const ceph::bufferlist& candidate,
                                    float *distance)
{
  ceph::bufferlist candidate_copy = candidate;
  const char *candidate_data = candidate_copy.c_str();
  return compute_float32_distance(
    req,
    query_values,
    query_norm_squared,
    std::string_view(candidate_data, candidate_copy.length()),
    distance);
}

inline int compute_int8_distance(const query_vectors_request_t& req,
                                    const std::vector<float>& query_values,
                                    double query_norm_squared,
                                    std::string_view candidate,
                                    float *distance)
{
  if (distance == nullptr) {
    return -EINVAL;
  }
  const size_t expected_len =
    static_cast<size_t>(req.dimension) * sizeof(int8_t);
  if (req.data_type != vector_data_type_int8 ||
      query_values.size() != req.dimension ||
      candidate.size() != expected_len) {
    return -EINVAL;
  }

  distance_terms_t terms;
  accumulate_distance_terms_int8(
      query_values.data(), candidate.data(), req.dimension, &terms);

  switch (req.distance_metric) {
  case vector_distance_metric_euclidean:
    *distance = static_cast<float>(std::sqrt(terms.sum_sq));
    return 0;
  case vector_distance_metric_cosine:
    if (query_norm_squared == 0 || terms.candidate_norm_squared == 0) {
      *distance = std::numeric_limits<float>::infinity();
    } else {
      *distance = static_cast<float>(
          1.0 - terms.dot /
          (std::sqrt(query_norm_squared) *
           std::sqrt(terms.candidate_norm_squared)));
    }
    return 0;
  case vector_distance_metric_dot:
    *distance = static_cast<float>(-terms.dot);
    return 0;
  default:
    return -EINVAL;
  }
}

// The same function as compute_int8_distance() above, over the integer
// kernels instead of the double ones. Validation, metric handling and
// error returns are line-for-line the reference's, so the only difference
// between the two is which kernel produced the terms -- and since both
// compute the same exact integers, not even the output differs.
inline int compute_int8_distance_fast(const query_vectors_request_t& req,
                                      const int8_fast_path_t& fast,
                                      double query_norm_squared,
                                      std::string_view candidate,
                                      float *distance)
{
  if (distance == nullptr) {
    return -EINVAL;
  }
  const size_t expected_len =
    static_cast<size_t>(req.dimension) * sizeof(int8_t);
  if (req.data_type != vector_data_type_int8 ||
      !fast.active() ||
      candidate.size() != expected_len) {
    return -EINVAL;
  }

  distance_terms_t terms;
  fast.compute_terms(candidate.data(), req.dimension, &terms);

  switch (req.distance_metric) {
  case vector_distance_metric_euclidean:
    *distance = static_cast<float>(std::sqrt(terms.sum_sq));
    return 0;
  case vector_distance_metric_cosine:
    if (query_norm_squared == 0 || terms.candidate_norm_squared == 0) {
      *distance = std::numeric_limits<float>::infinity();
    } else {
      *distance = static_cast<float>(
          1.0 - terms.dot /
          (std::sqrt(query_norm_squared) *
           std::sqrt(terms.candidate_norm_squared)));
    }
    return 0;
  case vector_distance_metric_dot:
    *distance = static_cast<float>(-terms.dot);
    return 0;
  default:
    return -EINVAL;
  }
}

// Picks the kernel from the request's data_type. float32 keeps the exact
// code path (and therefore the exact rounding) it had before int8 existed.
inline int compute_candidate_distance(const query_vectors_request_t& req,
                                      const std::vector<float>& query_values,
                                      double query_norm_squared,
                                      std::string_view candidate,
                                      float *distance)
{
  switch (req.data_type) {
  case vector_data_type_float32:
    return compute_float32_distance(
      req, query_values, query_norm_squared, candidate, distance);
  case vector_data_type_int8:
    return compute_int8_distance(
      req, query_values, query_norm_squared, candidate, distance);
  default:
    return -EINVAL;
  }
}

inline int compute_candidate_distance(const query_vectors_request_t& req,
                                      const std::vector<float>& query_values,
                                      double query_norm_squared,
                                      const ceph::bufferlist& candidate,
                                      float *distance)
{
  ceph::bufferlist candidate_copy = candidate;
  const char *candidate_data = candidate_copy.c_str();
  return compute_candidate_distance(
    req,
    query_values,
    query_norm_squared,
    std::string_view(candidate_data, candidate_copy.length()),
    distance);
}

inline const std::string& result_logical_identity(
    const query_vectors_result_entry_t& entry)
{
  // Partial results being merged belong to one request, which fixes the
  // bucket and index; the user key is therefore the logical identity.
  return entry.key;
}

inline bool result_entry_valid(const query_vectors_result_entry_t& entry)
{
  return !entry.key.empty() && !entry.entry_id.empty();
}

inline bool is_better_query_result(const query_vectors_result_entry_t& lhs,
                                   const query_vectors_result_entry_t& rhs)
{
  if (lhs.distance != rhs.distance) {
    return lhs.distance < rhs.distance;
  }
  return lhs.key < rhs.key;
}

inline void merge_result_entry(
    std::vector<query_vectors_result_entry_t> *retained_results,
    const query_vectors_result_entry_t& new_result)
{
  if (retained_results == nullptr) {
    return;
  }
  const std::string& new_result_id = result_logical_identity(new_result);
  if (new_result_id.empty()) {
    retained_results->push_back(new_result);
    return;
  }
  for (auto& retained_result : *retained_results) {
    if (result_logical_identity(retained_result) == new_result_id) {
      if (is_better_query_result(new_result, retained_result)) {
        retained_result = new_result;
      }
      return;
    }
  }
  retained_results->push_back(new_result);
}

inline void sort_and_trim_results(
    std::vector<query_vectors_result_entry_t> *retained_results,
    uint32_t top_k)
{
  if (retained_results == nullptr) {
    return;
  }
  std::sort(retained_results->begin(), retained_results->end(),
            is_better_query_result);
  if (top_k != 0 && retained_results->size() > top_k) {
    retained_results->resize(top_k);
  }
}

inline void retain_local_topk_result(
    std::vector<query_vectors_result_entry_t> *retained_results,
    const query_vectors_result_entry_t& new_result,
    uint32_t top_k,
    bool *is_heapified)
{
  if (retained_results == nullptr || is_heapified == nullptr) {
    return;
  }
  if (top_k == 0) {
    retained_results->push_back(new_result);
    return;
  }

  const size_t limit = top_k;
  if (retained_results->size() < limit) {
    retained_results->push_back(new_result);
    if (retained_results->size() == limit) {
      std::make_heap(retained_results->begin(), retained_results->end(),
                     is_better_query_result);
      *is_heapified = true;
    }
    return;
  }

  // With the "better" comparator, the heap front is the worst retained
  // result. Replace it only when the newly scanned result ranks better.
  if (is_better_query_result(new_result, retained_results->front())) {
    std::pop_heap(retained_results->begin(), retained_results->end(),
                  is_better_query_result);
    retained_results->back() = new_result;
    std::push_heap(retained_results->begin(), retained_results->end(),
                   is_better_query_result);
  }
}

inline void finalize_local_topk_results(
    std::vector<query_vectors_result_entry_t> *retained_results,
    uint32_t top_k,
    bool is_heapified)
{
  if (retained_results == nullptr) {
    return;
  }
  if (is_heapified) {
    std::sort_heap(retained_results->begin(), retained_results->end(),
                   is_better_query_result);
  } else {
    std::sort(retained_results->begin(), retained_results->end(),
              is_better_query_result);
  }
  if (top_k != 0 && retained_results->size() > top_k) {
    retained_results->resize(top_k);
  }
}

class local_query_accumulator_t {
public:
  int prepare(const query_vectors_request_t& query)
  {
    prepared = false;
    heapified = false;
    entries.clear();
    stats = query_filter_stats_t();
    query_values.clear();
    query_norm = 0;
    int8_fast = int8_fast_path_t();

    int r = validate_query_request(query);
    if (r < 0) {
      return r;
    }

    req = query;
    r = copy_query_values(
        req.data_type, req.query_vector, req.dimension, &query_values);
    if (r < 0) {
      return r;
    }
    query_norm = squared_l2_norm(query_values);
    // Opt-in integer kernels for int8. Returns false -- leaving every
    // distance to the reference path -- for float32, for a CPU or a
    // configuration without a fast kernel, and for a query whose values
    // are not exact int8 codes.
    int8_fast.prepare(req.data_type, query_values.data(), req.dimension,
                      1, req.dimension);

    prepared = true;
    return 0;
  }

  int consume(const vector_entry_view_t& entry)
  {
    return consume(entry, nullptr);
  }

  // Same as consume(), but the caller has already computed this entry's
  // distance -- the batched kernel does, from one pass over the stored
  // vectors (see accumulate_dots()/distance_from_terms()). Field checks,
  // request matching, duplicate collapse and top-k retention are the
  // single-query path unchanged.
  int consume(const vector_entry_view_t& entry, float distance)
  {
    return consume(entry, &distance);
  }

  int finish(query_vectors_result_t *result,
             query_filter_stats_t *out_stats = nullptr)
  {
    if (!prepared || result == nullptr) {
      return -EINVAL;
    }

    // A local scan visits each physical record once. Logical duplicate
    // merge happens when partial query results are combined.
    stats.merged_entries = stats.matched_entries;
    finalize_local_topk_results(&entries, req.local_top_k, heapified);
    stats.final_entries = entries.size();
    result->entries = std::move(entries);
    if (out_stats != nullptr) {
      *out_stats = stats;
    }
    prepared = false;
    return 0;
  }

  // The prepared query vector and its squared L2 norm. The batched kernel
  // copies these into one row-major B x D matrix so a candidate can be
  // driven against several queries without re-reading it per query.
  const std::vector<float>& prepared_query_values() const
  {
    return query_values;
  }
  double prepared_query_norm_squared() const
  {
    return query_norm;
  }

  // Whether this query is being answered by the int8 integer kernels
  // rather than the reference ones. Reported so a run can prove which
  // kernel it actually exercised instead of inferring it from timings.
  bool int8_fast_path_active() const
  {
    return int8_fast.active();
  }

  // Worst distance among the kept top-k, in raw non-squared L2, without
  // finalizing the accumulator. nullopt until local_top_k results have
  // been retained: before that there is no acceptance threshold and every
  // candidate still has to be considered.
  std::optional<float> current_tau() const
  {
    if (!heapified) {
      return std::nullopt;
    }
    return entries.front().distance;
  }

private:
  int consume(const vector_entry_view_t& entry, const float *precomputed)
  {
    if (!prepared) {
      return -EINVAL;
    }
    stats.total_entries++;
    if (!has_required_fields(entry)) {
      stats.incomplete_entries++;
      return 0;
    }
    if (!matches_request(entry)) {
      return 0;
    }
    accumulate_result(entry, precomputed);
    return 0;
  }

  static bool has_required_fields(const vector_entry_view_t& entry)
  {
    if (entry.entry_id.empty() ||
        !entry.has_vector_reference ||
        !entry.has_data_type ||
        !entry.has_distance_metric ||
        !entry.has_dimension) {
      return false;
    }
    // VectorNode-native rows never carry these (see is_vector_node_native);
    // every other source must have them.
    return entry.is_vector_node_native ||
      (!entry.bucket_name.empty() &&
       !entry.index_name.empty() &&
       !entry.user_key.empty());
  }

  bool matches_request(const vector_entry_view_t& entry)
  {
    // bucket_name/index_name/placement_key are unavailable for
    // VectorNode-native rows and redundant to check for them anyway (see
    // is_vector_node_native's comment).
    if (!entry.is_vector_node_native) {
      if (entry.bucket_name != req.bucket_name) {
        stats.bucket_mismatch++;
        return false;
      }
      if (entry.index_name != req.index_name) {
        stats.index_mismatch++;
        return false;
      }
    }
    if (entry.data_type != req.data_type) {
      stats.data_type_mismatch++;
      return false;
    }
    if (entry.distance_metric != req.distance_metric) {
      stats.distance_metric_mismatch++;
      return false;
    }
    if (entry.dimension != req.dimension) {
      stats.dimension_mismatch++;
      return false;
    }
    if (!entry.is_vector_node_native && !probe_matches(req, entry)) {
      stats.probe_mismatch++;
      return false;
    }
    return true;
  }

  void accumulate_result(const vector_entry_view_t& entry,
                         const float *precomputed)
  {
    if (!entry.has_vector_payload()) {
      stats.missing_content++;
      return;
    }
    if (entry.has_ambiguous_vector_payload()) {
      stats.distance_error++;
      return;
    }

    float distance = 0;
    stats.distance_computations++;
    int r = 0;
    if (precomputed != nullptr) {
      distance = *precomputed;
    } else {
      if (!entry.contiguous_vector_data.empty()) {
        r = int8_fast.active()
          ? compute_int8_distance_fast(
              req, int8_fast, query_norm,
              entry.contiguous_vector_data, &distance)
          : compute_candidate_distance(
              req, query_values, query_norm,
              entry.contiguous_vector_data, &distance);
      } else if (int8_fast.active()) {
        ceph::bufferlist candidate_copy = *entry.vector_data;
        const char *candidate_data = candidate_copy.c_str();
        r = compute_int8_distance_fast(
            req, int8_fast, query_norm,
            std::string_view(candidate_data, candidate_copy.length()),
            &distance);
      } else {
        r = compute_candidate_distance(
            req, query_values, query_norm, *entry.vector_data, &distance);
      }
    }
    if (r < 0) {
      // prepare() has already validated the request and query state. The
      // remaining failure is local to this record's vector payload.
      stats.distance_error++;
      return;
    }
    stats.matched_entries++;

    query_vectors_result_entry_t result_entry;
    result_entry.key = entry.user_key;
    result_entry.distance = distance;
    result_entry.entry_id = entry.entry_id;
    // Copy the vector only for a candidate that will enter (or replace an
    // entry of) the local top-k -- i.e. when the heap is not yet full, or
    // it beats the current worst -- not for every scanned candidate.
    if ((req.reply_flags & query_vectors_reply_include_vector) &&
        (!heapified || is_better_query_result(result_entry, entries.front()))) {
      if (!entry.contiguous_vector_data.empty()) {
        result_entry.vector.append(entry.contiguous_vector_data.data(),
                                   entry.contiguous_vector_data.size());
      } else {
        result_entry.vector = *entry.vector_data;
      }
    }

    // The put path is append-only (see append_vector_entry() in
    // vector_node.h) and entry_id is derived from
    // bucket_name/index_name/key, so one logical vector can have several
    // physical records. Without this check each copy takes its own top-k
    // slot, cutting the number of distinct vectors retained, and lets a
    // duplicate count towards heapified/current_tau() so tree-boundary
    // expansion stops before local_top_k unique vectors are found.
    for (auto& existing : entries) {
      if (existing.entry_id == result_entry.entry_id) {
        if (is_better_query_result(result_entry, existing)) {
          existing = result_entry;
          if (heapified) {
            std::make_heap(entries.begin(), entries.end(),
                           is_better_query_result);
          }
        }
        return;
      }
    }
    retain_local_topk_result(
        &entries, result_entry, req.local_top_k, &heapified);
  }

  query_vectors_request_t req;
  std::vector<float> query_values;
  double query_norm = 0;
  // Reference-preserving: inactive unless an int8 integer kernel is
  // selected AND this query encodes exactly (see prepare()).
  int8_fast_path_t int8_fast;
  std::vector<query_vectors_result_entry_t> entries;
  query_filter_stats_t stats;
  bool heapified = false;
  bool prepared = false;
};

inline vector_entry_view_t make_vector_entry_view(
    const ceph::os::vector_record_t& entry)
{
  vector_entry_view_t view;
  view.entry_id = entry.entry_id;
  view.bucket_name = entry.bucket_name;
  view.index_name = entry.index_name;
  view.user_key = entry.user_key;
  view.placement_key = entry.placement_key;
  view.data_type = entry.data_type;
  view.distance_metric = entry.distance_metric;
  view.dimension = entry.dimension;
  view.has_data_type = true;
  view.has_distance_metric = true;
  view.has_dimension = true;
  view.has_vector_reference = true;
  view.vector_data = &entry.vector_data;
  return view;
}

// Whether an expansion round narrowed the acceptance bound, for the
// best-first search's no-improvement termination check. current_tau() is
// nullopt until local_top_k results have been retained, meaning no bound
// exists yet rather than the worst possible one, so its absence must not
// count as no improvement:
//   tau_after == nullopt: top-k not filled yet, continue.
//   tau_before == nullopt and tau_after set: first bound, continue.
//   both set: continue only if tau_after < tau_before.
inline bool tree_boundary_tau_improved(
    std::optional<float> tau_before, std::optional<float> tau_after)
{
  if (!tau_after) {
    return true;
  }
  if (!tau_before) {
    return true;
  }
  return *tau_after < *tau_before;
}

} // namespace vector_query_exec
} // namespace rados
} // namespace ceph

#endif
