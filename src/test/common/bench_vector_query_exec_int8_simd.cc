// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

// Times the int8 distance kernels in isolation, so the cost the OSD's
// scan loop pays per candidate can be compared kernel to kernel without a
// cluster in the way. It calls exactly what seastore's batched scan calls:
// the reference pair (candidate_norm_squared_int8 + accumulate_dots_int8)
// against int8_fast_path_t::compute_dots().
//
// It also re-checks equality on every measured shape. A kernel that is
// fast and wrong should not be reportable as fast.
//
//   ceph_test_vector_int8_kernel_bench [candidates_per_query]

#include "common/vector_query_exec.h"

#include <chrono>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

namespace int8_simd = ceph::rados::vector_query_exec::int8_simd;
using ceph::rados::vector_data_type_int8;
using ceph::rados::vector_query_exec::accumulate_dots_int8;
using ceph::rados::vector_query_exec::candidate_norm_squared_int8;
using ceph::rados::vector_query_exec::int8_fast_path_t;
using int8_simd::kernel_t;

namespace {

int8_t pseudo_code(uint32_t seed)
{
  seed = seed * 1103515245u + 12345u;
  return static_cast<int8_t>(static_cast<int32_t>((seed >> 11) % 256) - 128);
}

struct measurement_t {
  double elapsed_us = 0;
  double checksum = 0;
};

// `records` is one contiguous block of candidates, as a LIST page is.
measurement_t run_reference(const std::vector<float>& matrix,
                            size_t query_count,
                            const std::vector<int8_t>& records,
                            size_t record_count,
                            uint32_t dimension)
{
  std::vector<double> dots(query_count, 0.0);
  double checksum = 0;
  const auto begin = std::chrono::steady_clock::now();
  for (size_t r = 0; r < record_count; ++r) {
    const char *bytes =
      reinterpret_cast<const char*>(records.data() + r * dimension);
    checksum += candidate_norm_squared_int8(bytes, dimension);
    accumulate_dots_int8(matrix.data(), dimension, query_count, bytes,
                         dimension, dots.data());
    for (size_t q = 0; q < query_count; ++q) {
      checksum += dots[q];
    }
  }
  const auto end = std::chrono::steady_clock::now();
  measurement_t m;
  m.elapsed_us =
    std::chrono::duration<double, std::micro>(end - begin).count();
  m.checksum = checksum;
  return m;
}

measurement_t run_fast(const int8_fast_path_t& fast,
                       size_t query_count,
                       const std::vector<int8_t>& records,
                       size_t record_count,
                       uint32_t dimension)
{
  std::vector<double> dots(query_count, 0.0);
  double checksum = 0;
  const auto begin = std::chrono::steady_clock::now();
  for (size_t r = 0; r < record_count; ++r) {
    const char *bytes =
      reinterpret_cast<const char*>(records.data() + r * dimension);
    checksum += fast.compute_dots(bytes, dimension, query_count, dots.data());
    for (size_t q = 0; q < query_count; ++q) {
      checksum += dots[q];
    }
  }
  const auto end = std::chrono::steady_clock::now();
  measurement_t m;
  m.elapsed_us =
    std::chrono::duration<double, std::micro>(end - begin).count();
  m.checksum = checksum;
  return m;
}

void bench_shape(uint32_t dimension, size_t query_count, size_t record_count,
                 const std::vector<kernel_t>& kernels)
{
  std::vector<float> matrix(query_count * dimension);
  for (size_t q = 0; q < query_count; ++q) {
    for (uint32_t i = 0; i < dimension; ++i) {
      matrix[q * dimension + i] =
        static_cast<float>(pseudo_code(uint32_t(q) * 7919 + i));
    }
  }
  std::vector<int8_t> records(record_count * dimension);
  for (size_t r = 0; r < record_count; ++r) {
    for (uint32_t i = 0; i < dimension; ++i) {
      records[r * dimension + i] =
        pseudo_code(uint32_t(r) * 104729 + i + 7);
    }
  }

  // One untimed pass so the records are resident for every kernel alike.
  const measurement_t warm =
    run_reference(matrix, query_count, records, record_count, dimension);
  const measurement_t reference =
    run_reference(matrix, query_count, records, record_count, dimension);
  (void)warm;

  const double pairs = double(record_count) * double(query_count);
  printf("  d=%-4u B=%-3zu  %-10s  %9.1f us  %7.2f ns/pair  %6s\n",
         dimension, query_count, "reference", reference.elapsed_us,
         reference.elapsed_us * 1000.0 / pairs, "1.00x");

  for (const kernel_t kernel : kernels) {
    if (kernel == kernel_t::reference) {
      continue;
    }
    int8_fast_path_t fast;
    {
      const kernel_t previous = int8_simd::selected_kernel();
      int8_simd::set_selected_kernel(kernel);
      const bool ok = fast.prepare(vector_data_type_int8, matrix.data(),
                                   dimension, query_count, dimension);
      int8_simd::set_selected_kernel(previous);
      if (!ok) {
        printf("  d=%-4u B=%-3zu  %-10s  (declined)\n",
               dimension, query_count, int8_simd::kernel_name(kernel));
        continue;
      }
    }
    const measurement_t warm_fast =
      run_fast(fast, query_count, records, record_count, dimension);
    const measurement_t measured =
      run_fast(fast, query_count, records, record_count, dimension);
    (void)warm_fast;
    const bool exact = measured.checksum == reference.checksum;
    printf("  d=%-4u B=%-3zu  %-10s  %9.1f us  %7.2f ns/pair  %5.2fx  %s\n",
           dimension, query_count, int8_simd::kernel_name(kernel),
           measured.elapsed_us, measured.elapsed_us * 1000.0 / pairs,
           reference.elapsed_us / measured.elapsed_us,
           exact ? "exact" : "MISMATCH");
  }
}

} // namespace

int main(int argc, char **argv)
{
  size_t record_count = 20000;
  if (argc > 1) {
    record_count = static_cast<size_t>(::atoll(argv[1]));
  }

  std::vector<kernel_t> kernels{kernel_t::reference};
  for (const kernel_t kernel : {kernel_t::avx2, kernel_t::vnni}) {
    if (int8_simd::kernel_available(kernel)) {
      kernels.push_back(kernel);
    }
  }

  printf("cpu: avx2=%d avx512vnni=%d\n",
         int(int8_simd::cpu_has_avx2()),
         int(int8_simd::cpu_has_avx512_vnni()));
  printf("auto-selected kernel: %s (CEPH_VECTOR_INT8_KERNEL -> %s)\n",
         int8_simd::kernel_name(int8_simd::best_available_kernel()),
         int8_simd::kernel_name(int8_simd::selected_kernel()));
  printf("records per measurement: %zu\n\n", record_count);

  for (const uint32_t dimension : {128u, 768u}) {
    printf("dimension %u\n", dimension);
    for (const size_t query_count : {size_t(1), size_t(4), size_t(8),
                                     size_t(16), size_t(32)}) {
      bench_shape(dimension, query_count, record_count, kernels);
    }
    printf("\n");
  }
  return 0;
}
