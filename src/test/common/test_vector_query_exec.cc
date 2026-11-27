// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#include "common/vector_query_exec.h"

#include <optional>

#include <gtest/gtest.h>

using ceph::rados::vector_query_exec::tree_boundary_tau_improved;

// current_tau() is nullopt until local_top_k results have been retained,
// meaning no bound exists yet rather than the worst possible one. The
// no-improvement termination check must not read that as no improvement,
// or query_vectors_tree_boundary() stops before top-k is filled.

TEST(VectorQueryExec, TopKStillUnfilledAlwaysContinues)
{
  // tau_after == nullopt: top-k not yet filled, regardless of tau_before.
  EXPECT_TRUE(tree_boundary_tau_improved(std::nullopt, std::nullopt));
  EXPECT_TRUE(tree_boundary_tau_improved(1.5f, std::nullopt));
}

TEST(VectorQueryExec, FirstBoundEstablishedContinues)
{
  // tau_before == nullopt, tau_after has a value: the bound was just
  // established for the first time.
  EXPECT_TRUE(tree_boundary_tau_improved(std::nullopt, 2.0f));
}

TEST(VectorQueryExec, BothPresentImprovedContinues)
{
  EXPECT_TRUE(tree_boundary_tau_improved(2.0f, 1.0f));
}

TEST(VectorQueryExec, BothPresentNoImprovementStops)
{
  EXPECT_FALSE(tree_boundary_tau_improved(1.0f, 1.0f));
  EXPECT_FALSE(tree_boundary_tau_improved(1.0f, 1.5f));
}

// The batched kernel answers several queries from one pass over a stored
// vector by reusing (||q||^2, ||v||^2, q.v). Its dot and candidate-norm
// accumulations keep the single-query kernel's lane order, so the cosine
// and dot metrics must come out bit-identical; euclidean derives
// ||q-v||^2 from the three terms instead of accumulating (q-v)^2, so it is
// only required to agree closely.

namespace {

using ceph::rados::query_vectors_request_t;
using ceph::rados::vector_data_type_float32;
using ceph::rados::vector_distance_metric_cosine;
using ceph::rados::vector_distance_metric_dot;
using ceph::rados::vector_distance_metric_euclidean;
using ceph::rados::vector_query_exec::accumulate_dots;
using ceph::rados::vector_query_exec::candidate_norm_squared;
using ceph::rados::vector_query_exec::compute_float32_distance;
using ceph::rados::vector_query_exec::distance_from_terms;
using ceph::rados::vector_query_exec::squared_l2_norm;

// Deterministic, and spread over a few orders of magnitude so the
// euclidean comparison is not flattered by tiny inputs.
float pseudo_random(uint32_t seed)
{
  seed = seed * 1103515245u + 12345u;
  return static_cast<float>((seed >> 9) % 4096) * 0.37f - 700.0f;
}

void check_batch_matches_single(size_t query_count, uint32_t dimension)
{
  std::vector<std::vector<float>> queries(query_count);
  std::vector<float> matrix(query_count * dimension);
  std::vector<double> norms(query_count);
  for (size_t q = 0; q < query_count; ++q) {
    queries[q].resize(dimension);
    for (uint32_t i = 0; i < dimension; ++i) {
      queries[q][i] = pseudo_random(q * 7919 + i);
      matrix[q * dimension + i] = queries[q][i];
    }
    norms[q] = squared_l2_norm(queries[q]);
  }
  std::vector<float> candidate(dimension);
  for (uint32_t i = 0; i < dimension; ++i) {
    candidate[i] = pseudo_random(104729 + i);
  }
  const char *candidate_bytes =
    reinterpret_cast<const char*>(candidate.data());
  const std::string_view candidate_view(
    candidate_bytes, dimension * sizeof(float));

  const double candidate_norm =
    candidate_norm_squared(candidate_bytes, dimension);
  std::vector<double> dots(query_count, 0.0);
  accumulate_dots(matrix.data(), dimension, query_count, candidate_bytes,
                  dimension, dots.data());

  for (const uint32_t metric : {vector_distance_metric_euclidean,
                                vector_distance_metric_cosine,
                                vector_distance_metric_dot}) {
    query_vectors_request_t req;
    req.dimension = dimension;
    req.data_type = vector_data_type_float32;
    req.distance_metric = metric;
    for (size_t q = 0; q < query_count; ++q) {
      float single = 0;
      ASSERT_EQ(0, compute_float32_distance(
          req, queries[q], norms[q], candidate_view, &single));
      float batched = 0;
      ASSERT_EQ(0, distance_from_terms(
          metric, norms[q], candidate_norm, dots[q], &batched));
      if (metric == vector_distance_metric_euclidean) {
        EXPECT_NEAR(single, batched, std::abs(single) * 1e-5f + 1e-3f)
            << "query " << q << " dim " << dimension;
      } else {
        EXPECT_EQ(single, batched)
            << "metric " << metric << " query " << q
            << " dim " << dimension;
      }
    }
  }
}

} // namespace

TEST(VectorQueryExec, BatchedKernelMatchesSingleQueryKernel)
{
  // 128 is the SIFT dimension and a multiple of both the 4-wide SIMD step
  // and the 4-query block; 13 exercises the scalar tail; 3 and 6 queries
  // exercise the block remainder.
  check_batch_matches_single(8, 128);
  check_batch_matches_single(3, 128);
  check_batch_matches_single(6, 13);
  check_batch_matches_single(1, 13);
}
