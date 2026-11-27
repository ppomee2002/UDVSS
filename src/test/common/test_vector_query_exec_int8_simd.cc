// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#include "common/vector_query_exec.h"

#include <cstdint>
#include <string>
#include <vector>

#include <gtest/gtest.h>

// The int8 integer kernels are a second implementation of kernels that
// already exist, so the whole of their contract is "indistinguishable
// from the reference". Both sides work on small integers, and every
// intermediate stays far below 2^53, so the reference's doubles are exact
// too: these tests therefore assert EXACT equality, not a tolerance. A
// tolerance here would hide precisely the bug worth catching -- a
// sign-extension or saturation slip that moves one distance and so
// reorders one neighbour.

namespace {

using ceph::rados::query_vectors_request_t;
using ceph::rados::vector_data_type_float32;
using ceph::rados::vector_data_type_int8;
using ceph::rados::vector_distance_metric_cosine;
using ceph::rados::vector_distance_metric_dot;
using ceph::rados::vector_distance_metric_euclidean;
using ceph::rados::vector_query_exec::accumulate_dots_int8;
using ceph::rados::vector_query_exec::candidate_norm_squared_int8;
using ceph::rados::vector_query_exec::compute_int8_distance;
using ceph::rados::vector_query_exec::compute_int8_distance_fast;
using ceph::rados::vector_query_exec::distance_from_terms;
using ceph::rados::vector_query_exec::int8_fast_path_t;
using ceph::rados::vector_query_exec::local_query_accumulator_t;
using ceph::rados::vector_query_exec::squared_l2_norm;
using ceph::rados::vector_query_exec::vector_entry_view_t;

namespace int8_simd = ceph::rados::vector_query_exec::int8_simd;
using int8_simd::kernel_t;

// Deterministic int8 codes spanning the full signed range, including both
// end points: -128 is the asymmetric one that a sign-extension bug turns
// into +128, and 127 is where the unsigned reinterpretation of a negative
// code would land.
int8_t pseudo_code(uint32_t seed)
{
  seed = seed * 1103515245u + 12345u;
  return static_cast<int8_t>(static_cast<int32_t>((seed >> 11) % 256) - 128);
}

// A quantized query as the OSD sees it: floats that hold integer codes.
std::vector<float> make_query(uint32_t dimension, uint32_t seed)
{
  std::vector<float> query(dimension);
  for (uint32_t i = 0; i < dimension; ++i) {
    query[i] = static_cast<float>(pseudo_code(seed * 7919 + i));
  }
  return query;
}

std::vector<int8_t> make_candidate(uint32_t dimension, uint32_t seed)
{
  std::vector<int8_t> candidate(dimension);
  for (uint32_t i = 0; i < dimension; ++i) {
    candidate[i] = pseudo_code(seed * 104729 + i);
  }
  return candidate;
}

std::string_view candidate_view(const std::vector<int8_t>& candidate)
{
  return std::string_view(
      reinterpret_cast<const char*>(candidate.data()), candidate.size());
}

// Every kernel this CPU can actually run, reference always included.
std::vector<kernel_t> runnable_kernels()
{
  std::vector<kernel_t> kernels{kernel_t::reference};
  for (const kernel_t kernel : {kernel_t::avx2, kernel_t::vnni}) {
    if (int8_simd::kernel_available(kernel)) {
      kernels.push_back(kernel);
    }
  }
  return kernels;
}

// Restores whatever kernel was selected, so one test forcing a kernel
// cannot change what the next one measures.
class scoped_kernel_t {
public:
  explicit scoped_kernel_t(kernel_t kernel)
    : previous_(int8_simd::selected_kernel())
  {
    int8_simd::set_selected_kernel(kernel);
  }
  ~scoped_kernel_t() { int8_simd::set_selected_kernel(previous_); }
private:
  kernel_t previous_;
};

} // namespace

// ---- selection and feature detection --------------------------------

TEST(VectorQueryExecInt8Simd, KernelNamesRoundTrip)
{
  bool recognized = false;
  EXPECT_EQ(kernel_t::reference,
            int8_simd::parse_kernel_name("reference", &recognized));
  EXPECT_TRUE(recognized);
  EXPECT_EQ(kernel_t::avx2, int8_simd::parse_kernel_name("avx2", &recognized));
  EXPECT_TRUE(recognized);
  EXPECT_EQ(kernel_t::vnni, int8_simd::parse_kernel_name("vnni", &recognized));
  EXPECT_TRUE(recognized);

  // An unrecognized name must not be read as a kernel choice.
  int8_simd::parse_kernel_name("sse42", &recognized);
  EXPECT_FALSE(recognized);

  EXPECT_STREQ("reference", int8_simd::kernel_name(kernel_t::reference));
  EXPECT_STREQ("avx2", int8_simd::kernel_name(kernel_t::avx2));
  EXPECT_STREQ("vnni", int8_simd::kernel_name(kernel_t::vnni));
}

TEST(VectorQueryExecInt8Simd, SelectionNeverExceedsCpuSupport)
{
  // The reference is always available; a fast kernel is offered only when
  // the CPU probe says so, and best_available_kernel() never names one it
  // cannot run.
  EXPECT_TRUE(int8_simd::kernel_available(kernel_t::reference));
  const kernel_t best = int8_simd::best_available_kernel();
  EXPECT_TRUE(int8_simd::kernel_available(best));
  if (!int8_simd::cpu_has_avx2()) {
    EXPECT_EQ(kernel_t::reference, best);
  }
  if (!int8_simd::cpu_has_avx512_vnni()) {
    EXPECT_NE(kernel_t::vnni, best);
  }
  // VNNI without AVX2 would be a detection bug: the dispatcher computes
  // ||v||^2 with the AVX2 kernel for either fast path.
  if (int8_simd::cpu_has_avx512_vnni()) {
    EXPECT_TRUE(int8_simd::cpu_has_avx2());
  }
}

TEST(VectorQueryExecInt8Simd, ReferenceSelectionLeavesFastPathInactive)
{
  scoped_kernel_t scoped(kernel_t::reference);
  int8_fast_path_t fast;
  const std::vector<float> query = make_query(128, 1);
  EXPECT_FALSE(fast.prepare(vector_data_type_int8, query.data(), 128, 1, 128));
  EXPECT_FALSE(fast.active());
}

// float32 must never reach this path, whatever is selected.
TEST(VectorQueryExecInt8Simd, Float32NeverTakesTheFastPath)
{
  for (const kernel_t kernel : runnable_kernels()) {
    scoped_kernel_t scoped(kernel);
    int8_fast_path_t fast;
    const std::vector<float> query = make_query(128, 2);
    EXPECT_FALSE(
        fast.prepare(vector_data_type_float32, query.data(), 128, 1, 128));
    EXPECT_FALSE(fast.active());
  }
}

// A query that is not a set of int8 codes cannot be encoded, so the fast
// path must decline it rather than round it.
TEST(VectorQueryExecInt8Simd, NonIntegralOrOutOfRangeQueryDeclines)
{
  for (const kernel_t kernel : runnable_kernels()) {
    if (kernel == kernel_t::reference) {
      continue;
    }
    scoped_kernel_t scoped(kernel);
    const uint32_t dimension = 64;

    std::vector<float> fractional = make_query(dimension, 3);
    fractional[7] = 12.5f;
    int8_fast_path_t fast;
    EXPECT_FALSE(fast.prepare(vector_data_type_int8, fractional.data(),
                              dimension, 1, dimension));
    EXPECT_FALSE(fast.active());

    std::vector<float> too_large = make_query(dimension, 4);
    too_large[3] = 1000.0f;
    EXPECT_FALSE(fast.prepare(vector_data_type_int8, too_large.data(),
                              dimension, 1, dimension));

    std::vector<float> below_range = make_query(dimension, 5);
    below_range[11] = -129.0f;
    EXPECT_FALSE(fast.prepare(vector_data_type_int8, below_range.data(),
                              dimension, 1, dimension));

    // An absurd dimension must fall back rather than risk the int32 lane
    // accumulators; a legal one at the cap boundary must still be taken.
    std::vector<float> ok = make_query(dimension, 6);
    EXPECT_TRUE(fast.prepare(vector_data_type_int8, ok.data(), dimension,
                             1, dimension));
    EXPECT_FALSE(fast.prepare(vector_data_type_int8, ok.data(), dimension,
                              1, int8_simd::max_safe_dimension + 1));
  }
}

// ---- single-query equality ------------------------------------------

namespace {

void check_single_matches_reference(kernel_t kernel, uint32_t dimension,
                                    uint32_t seed)
{
  scoped_kernel_t scoped(kernel);
  const std::vector<float> query = make_query(dimension, seed);
  const std::vector<int8_t> candidate = make_candidate(dimension, seed + 1);
  const double query_norm = squared_l2_norm(query);

  int8_fast_path_t fast;
  ASSERT_TRUE(fast.prepare(vector_data_type_int8, query.data(), dimension,
                           1, dimension))
      << "kernel " << int8_simd::kernel_name(kernel)
      << " dim " << dimension;

  for (const uint32_t metric : {vector_distance_metric_euclidean,
                                vector_distance_metric_cosine,
                                vector_distance_metric_dot}) {
    query_vectors_request_t req;
    req.dimension = dimension;
    req.data_type = vector_data_type_int8;
    req.distance_metric = metric;

    float reference = 0;
    ASSERT_EQ(0, compute_int8_distance(
        req, query, query_norm, candidate_view(candidate), &reference));
    float fast_result = 0;
    ASSERT_EQ(0, compute_int8_distance_fast(
        req, fast, query_norm, candidate_view(candidate), &fast_result));
    EXPECT_EQ(reference, fast_result)
        << "kernel " << int8_simd::kernel_name(kernel)
        << " metric " << metric << " dim " << dimension;
  }
}

} // namespace

TEST(VectorQueryExecInt8Simd, SingleQueryMatchesReferenceExactly)
{
  for (const kernel_t kernel : runnable_kernels()) {
    if (kernel == kernel_t::reference) {
      continue;
    }
    // 128 is SIFT and 768 is Cohere; 32 and 16 are the exact VNNI and
    // AVX2 step widths; 31/33/17/13/1 land on either side of them so the
    // scalar tails and the VNNI bias correction over a partial prefix are
    // all exercised. 7 is shorter than one step of either kernel, so the
    // whole computation is the tail.
    for (const uint32_t dimension : {128u, 768u, 32u, 33u, 31u, 16u, 17u,
                                     13u, 7u, 1u}) {
      check_single_matches_reference(kernel, dimension, dimension);
    }
  }
}

// ---- batched equality ------------------------------------------------

namespace {

void check_batch_matches_reference(kernel_t kernel, size_t query_count,
                                   uint32_t dimension)
{
  scoped_kernel_t scoped(kernel);
  std::vector<float> matrix(query_count * dimension);
  std::vector<double> norms(query_count);
  for (size_t q = 0; q < query_count; ++q) {
    const std::vector<float> query =
      make_query(dimension, static_cast<uint32_t>(q) + 41);
    std::copy(query.begin(), query.end(), matrix.begin() + q * dimension);
    norms[q] = squared_l2_norm(query);
  }
  const std::vector<int8_t> candidate = make_candidate(dimension, 997);
  const char *candidate_bytes =
    reinterpret_cast<const char*>(candidate.data());

  int8_fast_path_t fast;
  ASSERT_TRUE(fast.prepare(vector_data_type_int8, matrix.data(), dimension,
                           query_count, dimension));

  const double reference_norm =
    candidate_norm_squared_int8(candidate_bytes, dimension);
  std::vector<double> reference_dots(query_count, 0.0);
  accumulate_dots_int8(matrix.data(), dimension, query_count,
                       candidate_bytes, dimension, reference_dots.data());

  std::vector<double> fast_dots(query_count, 0.0);
  const double fast_norm = fast.compute_dots(
      candidate_bytes, dimension, query_count, fast_dots.data());

  EXPECT_EQ(reference_norm, fast_norm)
      << "kernel " << int8_simd::kernel_name(kernel)
      << " dim " << dimension;
  for (size_t q = 0; q < query_count; ++q) {
    EXPECT_EQ(reference_dots[q], fast_dots[q])
        << "kernel " << int8_simd::kernel_name(kernel)
        << " query " << q << " dim " << dimension;
    // The distances the scan path actually reports must agree too, for
    // every metric -- that is what recall is computed from.
    for (const uint32_t metric : {vector_distance_metric_euclidean,
                                  vector_distance_metric_cosine,
                                  vector_distance_metric_dot}) {
      float reference = 0;
      float fast_result = 0;
      ASSERT_EQ(0, distance_from_terms(metric, norms[q], reference_norm,
                                       reference_dots[q], &reference));
      ASSERT_EQ(0, distance_from_terms(metric, norms[q], fast_norm,
                                       fast_dots[q], &fast_result));
      EXPECT_EQ(reference, fast_result)
          << "kernel " << int8_simd::kernel_name(kernel)
          << " metric " << metric << " query " << q;
    }
  }
}

} // namespace

TEST(VectorQueryExecInt8Simd, BatchedMatchesReferenceExactly)
{
  for (const kernel_t kernel : runnable_kernels()) {
    if (kernel == kernel_t::reference) {
      continue;
    }
    // Query counts around the AVX2 kernel's four-row block: 8 is two full
    // blocks, 3 is block remainder only, 6 is one block plus a remainder,
    // 1 is the single-row loop.
    for (const size_t query_count : {size_t(8), size_t(3), size_t(6),
                                     size_t(1)}) {
      for (const uint32_t dimension : {128u, 768u, 33u, 13u}) {
        check_batch_matches_reference(kernel, query_count, dimension);
      }
    }
  }
}

// ---- overflow, signedness, saturation --------------------------------

// The saturating instruction a byte dot product invites (VPMADDUBSW) would
// clamp here; the kernels avoid it. All-extreme inputs at a large
// dimension drive the int32 lane accumulators as hard as the kernels
// permit, and the expected values are computed independently in int64.
TEST(VectorQueryExecInt8Simd, ExtremeCodesDoNotSaturateOrOverflow)
{
  for (const kernel_t kernel : runnable_kernels()) {
    if (kernel == kernel_t::reference) {
      continue;
    }
    scoped_kernel_t scoped(kernel);
    // Large enough that a per-lane int16 or a naive int32 accumulation
    // would already have wrapped, and every element is an extreme.
    const uint32_t dimension = 8192;
    std::vector<float> query(dimension);
    std::vector<int8_t> candidate(dimension);
    for (uint32_t i = 0; i < dimension; ++i) {
      // Alternating signs on both sides: the products alternate in sign,
      // so a sign-extension slip changes the total rather than cancelling.
      query[i] = (i % 2 == 0) ? 127.0f : -128.0f;
      candidate[i] = (i % 3 == 0) ? int8_t(-128) : int8_t(127);
    }

    int64_t expected_dot = 0;
    int64_t expected_norm = 0;
    int64_t expected_sum_sq = 0;
    for (uint32_t i = 0; i < dimension; ++i) {
      const int64_t q = static_cast<int64_t>(query[i]);
      const int64_t c = candidate[i];
      expected_dot += q * c;
      expected_norm += c * c;
      expected_sum_sq += (q - c) * (q - c);
    }

    int8_fast_path_t fast;
    ASSERT_TRUE(fast.prepare(vector_data_type_int8, query.data(), dimension,
                             1, dimension));
    std::vector<double> dots(1, 0.0);
    const double norm = fast.compute_dots(
        reinterpret_cast<const char*>(candidate.data()), dimension, 1,
        dots.data());
    EXPECT_EQ(static_cast<double>(expected_dot), dots[0])
        << "kernel " << int8_simd::kernel_name(kernel);
    EXPECT_EQ(static_cast<double>(expected_norm), norm)
        << "kernel " << int8_simd::kernel_name(kernel);

    query_vectors_request_t req;
    req.dimension = dimension;
    req.data_type = vector_data_type_int8;
    req.distance_metric = vector_distance_metric_euclidean;
    float fast_result = 0;
    ASSERT_EQ(0, compute_int8_distance_fast(
        req, fast, static_cast<double>(squared_l2_norm(query)),
        candidate_view(candidate), &fast_result));
    EXPECT_EQ(static_cast<float>(std::sqrt(
                  static_cast<double>(expected_sum_sq))),
              fast_result)
        << "kernel " << int8_simd::kernel_name(kernel);

    // And the reference agrees with the independent int64 arithmetic too,
    // which is what makes "equal to the reference" worth asserting.
    float reference = 0;
    ASSERT_EQ(0, compute_int8_distance(
        req, query, static_cast<double>(squared_l2_norm(query)),
        candidate_view(candidate), &reference));
    EXPECT_EQ(reference, fast_result);
  }
}

// The VNNI bias correction is subtracted per candidate from sum(v). A
// candidate whose codes sum to zero hides a wrong correction, and one
// whose codes are all the same maximises it, so check both explicitly.
TEST(VectorQueryExecInt8Simd, BiasCorrectionHoldsForExtremeCandidateSums)
{
  for (const kernel_t kernel : runnable_kernels()) {
    if (kernel == kernel_t::reference) {
      continue;
    }
    scoped_kernel_t scoped(kernel);
    // 100 is not a multiple of the 32-wide VNNI step, so the correction
    // has to be taken over the vectorized prefix alone.
    const uint32_t dimension = 100;
    const std::vector<float> query = make_query(dimension, 31337);
    const double query_norm = squared_l2_norm(query);

    std::vector<std::vector<int8_t>> candidates;
    candidates.emplace_back(dimension, int8_t(0));
    candidates.emplace_back(dimension, int8_t(127));
    candidates.emplace_back(dimension, int8_t(-128));
    std::vector<int8_t> zero_sum(dimension);
    for (uint32_t i = 0; i < dimension; ++i) {
      zero_sum[i] = (i % 2 == 0) ? int8_t(64) : int8_t(-64);
    }
    candidates.push_back(zero_sum);

    int8_fast_path_t fast;
    ASSERT_TRUE(fast.prepare(vector_data_type_int8, query.data(), dimension,
                             1, dimension));

    query_vectors_request_t req;
    req.dimension = dimension;
    req.data_type = vector_data_type_int8;
    req.distance_metric = vector_distance_metric_dot;
    for (const auto& candidate : candidates) {
      float reference = 0;
      ASSERT_EQ(0, compute_int8_distance(
          req, query, query_norm, candidate_view(candidate), &reference));
      float fast_result = 0;
      ASSERT_EQ(0, compute_int8_distance_fast(
          req, fast, query_norm, candidate_view(candidate), &fast_result));
      EXPECT_EQ(reference, fast_result)
          << "kernel " << int8_simd::kernel_name(kernel);
    }
  }
}

// ---- end-to-end: the results a scan actually returns -----------------

namespace {

vector_entry_view_t make_entry(const query_vectors_request_t& req,
                               const std::string& entry_id,
                               const std::string& key,
                               std::string_view payload)
{
  vector_entry_view_t entry;
  entry.entry_id = entry_id;
  entry.user_key = key;
  entry.bucket_name = req.bucket_name;
  entry.index_name = req.index_name;
  entry.data_type = req.data_type;
  entry.distance_metric = req.distance_metric;
  entry.dimension = req.dimension;
  entry.has_data_type = true;
  entry.has_distance_metric = true;
  entry.has_dimension = true;
  entry.has_vector_reference = true;
  entry.contiguous_vector_data = payload;
  return entry;
}

} // namespace

// The recall-invariance argument, at unit scale: run the same scan twice
// through local_query_accumulator_t, once with the reference kernels and
// once with an integer kernel, and require the retained top-k to be the
// same entries in the same order with the same distances. Anything that
// could move recall in the cluster benchmark would have to show up here.
TEST(VectorQueryExecInt8Simd, AccumulatorTopKIsIdenticalToReference)
{
  const uint32_t dimension = 768;
  const size_t record_count = 500;
  std::vector<std::vector<int8_t>> records;
  std::vector<std::string> entry_ids;
  std::vector<std::string> keys;
  records.reserve(record_count);
  for (size_t i = 0; i < record_count; ++i) {
    records.push_back(make_candidate(dimension, static_cast<uint32_t>(i) + 3));
    entry_ids.push_back("entry-" + std::to_string(i));
    keys.push_back("key-" + std::to_string(i));
  }
  const std::vector<float> query = make_query(dimension, 777);

  for (const uint32_t metric : {vector_distance_metric_euclidean,
                                vector_distance_metric_cosine,
                                vector_distance_metric_dot}) {
    query_vectors_request_t req;
    req.bucket_name = "bucket";
    req.index_name = "index";
    req.dimension = dimension;
    req.data_type = vector_data_type_int8;
    req.distance_metric = metric;
    req.local_top_k = 10;
    req.query_vector.clear();
    for (const float value : query) {
      int8_t code = static_cast<int8_t>(value);
      req.query_vector.append(reinterpret_cast<const char*>(&code),
                              sizeof(code));
    }

    std::vector<ceph::rados::query_vectors_result_t> results;
    for (const kernel_t kernel : runnable_kernels()) {
      scoped_kernel_t scoped(kernel);
      local_query_accumulator_t accumulator;
      ASSERT_EQ(0, accumulator.prepare(req));
      for (size_t i = 0; i < record_count; ++i) {
        ASSERT_EQ(0, accumulator.consume(make_entry(
            req, entry_ids[i], keys[i], candidate_view(records[i]))));
      }
      ceph::rados::query_vectors_result_t result;
      ASSERT_EQ(0, accumulator.finish(&result));
      results.push_back(std::move(result));
    }

    ASSERT_FALSE(results.empty());
    ASSERT_EQ(size_t(10), results[0].entries.size());
    for (size_t k = 1; k < results.size(); ++k) {
      ASSERT_EQ(results[0].entries.size(), results[k].entries.size());
      for (size_t i = 0; i < results[0].entries.size(); ++i) {
        EXPECT_EQ(results[0].entries[i].entry_id, results[k].entries[i].entry_id)
            << "metric " << metric << " rank " << i;
        EXPECT_EQ(results[0].entries[i].distance, results[k].entries[i].distance)
            << "metric " << metric << " rank " << i;
      }
    }
  }
}
