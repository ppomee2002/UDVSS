// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#ifndef CEPH_COMMON_VECTOR_QUERY_EXEC_INT8_SIMD_H
#define CEPH_COMMON_VECTOR_QUERY_EXEC_INT8_SIMD_H

// Integer fast path for vector_data_type_int8.
//
// vector_query_exec.h already has int8 kernels. They widen every code to
// double and reuse the float32 kernels' shape, which is what makes them a
// trustworthy reference: they are the float32 arithmetic applied to the
// same numbers. They are also four times narrower than they need to be --
// an AVX2 register holds four doubles but thirty-two int8 codes.
//
// This header adds a second implementation that keeps the codes as
// integers. It does not replace the reference: the caller picks one at
// run time (see selected_kernel()), and the reference is what runs when
// this path is unavailable, disabled, or handed a query it cannot encode.
//
// EXACTNESS
//
// Both sides of an int8 comparison are small integers -- the client
// applies one global symmetric scale to the stored vectors and to the
// query alike, so the query floats the OSD sees hold integer codes. Every
// intermediate here (dot, ||v||^2, ||q-v||^2) is therefore an integer
// whose magnitude stays far below 2^53, so the reference path's doubles
// hold them exactly too. The integer kernels below are not an
// approximation of the reference: they compute the same integers, so the
// resulting distances are bit-identical and recall cannot move. The unit
// test asserts exact equality rather than a tolerance.
//
// SIGNEDNESS AND SATURATION
//
// The int8 codes are SIGNED. Three traps follow from that, all handled
// here and all worth stating because getting one wrong is silent:
//
//   1. _mm256_maddubs_epi16 is the obvious "byte dot product" and is
//      WRONG for this: it saturates its int16 output. Its operands are
//      u8 x s8, so one output element can reach 255*127 + 255*127 =
//      64770, which clamps to 32767. It is not used anywhere below.
//
//   2. The AVX2 kernel sign-extends to int16 (_mm256_cvtepi8_epi16) and
//      uses _mm256_madd_epi16, which produces int32 and cannot saturate
//      for our range: the only saturating input pair is (-32768,-32768),
//      unreachable from int8.
//
//   3. VPDPBUSD (_mm256_dpbusd_epi32) takes its FIRST operand as
//      UNSIGNED bytes. Feeding it signed query codes would read -1 as
//      255. The query is therefore biased by +128 into [0,255] once, at
//      prepare time, and the bias is removed per candidate:
//
//        sum_i (q_i + 128) * v_i = q.v + 128 * sum_i v_i
//
//      so q.v = dpbusd_result - 128 * candidate_code_sum, where
//      candidate_code_sum is computed once per candidate alongside
//      ||v||^2 and shared by every query in a batch. VPDPBUSD (not the
//      VPDPBUSDS variant) accumulates into int32 without saturating.
//
// ACCUMULATOR RANGE
//
// With |q|,|v| <= 128 each product is <= 16384.
//   AVX2: one madd lane holds two products, <= 32768 per iteration, over
//         dimension/16 iterations.
//   VNNI: one lane holds four u8 x s8 products, <= 255*128*4 = 130560 per
//         iteration, over dimension/32 iterations.
// Both stay inside int32 well past any real dimension; max_safe_dimension
// below is the conservative cap, and a larger dimension falls back to the
// reference path rather than wrapping.

#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
#include "arch/probe.h"
#include "arch/intel.h"
#define CEPH_VECTOR_INT8_SIMD_X86 1
#endif

namespace ceph {
namespace rados {
namespace vector_query_exec {
namespace int8_simd {

// `reference` means "not this header": the caller keeps using the double
// kernels in vector_query_exec.h. It is the value returned whenever no
// fast path is usable, so a caller only has to test for it.
enum class kernel_t {
  reference = 0,
  avx2 = 1,
  vnni = 2,
};

inline const char *kernel_name(kernel_t kernel)
{
  switch (kernel) {
  case kernel_t::avx2: return "avx2";
  case kernel_t::vnni: return "vnni";
  case kernel_t::reference:
  default: return "reference";
  }
}

// Above this the int32 lane accumulators would eventually need checking;
// rather than check per element, refuse the dimension and let the caller
// fall back. Real indexes are three orders of magnitude below it.
inline constexpr uint32_t max_safe_dimension = 262144;

// ---- CPU feature detection ------------------------------------------

inline bool cpu_has_avx2()
{
#if defined(CEPH_VECTOR_INT8_SIMD_X86)
  static const bool detected = [] {
    ceph_arch_probe();
    return ceph_arch_intel_avx2 != 0;
  }();
  return detected;
#else
  return false;
#endif
}

inline bool cpu_has_avx512_vnni()
{
#if defined(CEPH_VECTOR_INT8_SIMD_X86)
  static const bool detected = [] {
    ceph_arch_probe();
    return ceph_arch_intel_avx512_vnni != 0;
  }();
  return detected;
#else
  return false;
#endif
}

inline bool kernel_available(kernel_t kernel)
{
  switch (kernel) {
  case kernel_t::avx2: return cpu_has_avx2();
  // The 256-bit VPDPBUSD form is EVEX-encoded, so it needs AVX512VL as
  // well as AVX512VNNI; ceph_arch_intel_avx512_vnni checks both (and
  // AVX512F/BW, and that the OS has enabled the AVX512 state).
  case kernel_t::vnni: return cpu_has_avx512_vnni();
  case kernel_t::reference: return true;
  }
  return false;
}

// ---- kernel selection ------------------------------------------------
//
// CEPH_VECTOR_INT8_KERNEL=reference|avx2|vnni|auto picks the kernel at
// run time. It is read once. An explicit choice the CPU cannot run falls
// back rather than crashing, so the variable is safe to set uniformly
// across a heterogeneous cluster.

inline kernel_t parse_kernel_name(const std::string& name, bool *recognized)
{
  if (recognized != nullptr) {
    *recognized = true;
  }
  if (name == "reference" || name == "ref" || name == "off" || name == "0") {
    return kernel_t::reference;
  }
  if (name == "avx2") {
    return kernel_t::avx2;
  }
  if (name == "vnni" || name == "avx512vnni") {
    return kernel_t::vnni;
  }
  if (recognized != nullptr) {
    *recognized = false;
  }
  return kernel_t::reference;
}

inline kernel_t best_available_kernel()
{
  if (kernel_available(kernel_t::vnni)) {
    return kernel_t::vnni;
  }
  if (kernel_available(kernel_t::avx2)) {
    return kernel_t::avx2;
  }
  return kernel_t::reference;
}

inline kernel_t resolve_kernel_from_env()
{
  const char *requested = ::getenv("CEPH_VECTOR_INT8_KERNEL");
  if (requested == nullptr || ::strcmp(requested, "auto") == 0) {
    return best_available_kernel();
  }
  bool recognized = false;
  const kernel_t parsed = parse_kernel_name(requested, &recognized);
  if (!recognized) {
    return best_available_kernel();
  }
  if (!kernel_available(parsed)) {
    // Asked for something this CPU does not have. Do not silently
    // upgrade to a different fast path either -- the operator named a
    // kernel, so anything but that kernel is the reference.
    return kernel_t::reference;
  }
  return parsed;
}

// Test/bench hook: forces a kernel for the rest of the process. Nothing
// on the serving path calls this; selected_kernel() is otherwise fixed by
// the environment at first use.
inline kernel_t *kernel_override_slot()
{
  static kernel_t *slot = nullptr;
  return slot ? slot : (slot = new kernel_t(resolve_kernel_from_env()));
}

inline kernel_t selected_kernel()
{
  return *kernel_override_slot();
}

inline void set_selected_kernel(kernel_t kernel)
{
  *kernel_override_slot() = kernel;
}

// ---- exact integer terms ---------------------------------------------

// The integer twin of distance_terms_t. Every field is an exact integer,
// which is why converting it to the double-valued distance_terms_t loses
// nothing.
struct terms_t {
  int64_t dot = 0;
  int64_t candidate_norm_squared = 0;
  int64_t sum_sq = 0;
};

// Per-candidate quantities that do not depend on the query, so a batch
// pays for them once per record rather than once per (query, record):
// ||v||^2 for every metric, and sum(v) for the VNNI bias correction.
struct candidate_stats_t {
  int64_t norm_squared = 0;
  int32_t code_sum = 0;
};

// ---- prepared query codes --------------------------------------------

// The fast paths need the query as integers, and VNNI needs it biased
// into unsigned bytes. Both are derived once per batch from the floats
// the accumulator already prepared, so the per-candidate loop never
// re-encodes anything.
//
// build() returns false when the query is not a quantized one -- a
// non-integral value, or one outside int8 -- which is the caller's signal
// to use the reference path. That is a correctness gate, not an
// optimization: a fractional query code cannot be represented here.
class query_codes_t {
public:
  bool build(kernel_t kernel,
             const float *rows,
             size_t stride,
             size_t row_count,
             uint32_t dimension)
  {
    reset();
    if (kernel == kernel_t::reference ||
        rows == nullptr || row_count == 0 ||
        dimension == 0 || dimension > max_safe_dimension ||
        stride < dimension) {
      return false;
    }

    kernel_ = kernel;
    row_count_ = row_count;
    dimension_ = dimension;
    const bool need_biased = (kernel == kernel_t::vnni);

    codes_.resize(row_count * dimension);
    if (need_biased) {
      biased_.resize(row_count * dimension);
    }
    norm_squared_.assign(row_count, 0);

    for (size_t r = 0; r < row_count; ++r) {
      const float *row = rows + r * stride;
      int64_t norm = 0;
      for (uint32_t i = 0; i < dimension; ++i) {
        const float value = row[i];
        const int32_t code = static_cast<int32_t>(value);
        // Rejects NaN, fractions and anything outside int8 in one test:
        // the round trip only reproduces `value` for an exact integer.
        if (static_cast<float>(code) != value ||
            code < -128 || code > 127) {
          reset();
          return false;
        }
        codes_[r * dimension + i] = static_cast<int16_t>(code);
        if (need_biased) {
          biased_[r * dimension + i] = static_cast<uint8_t>(code + 128);
        }
        norm += static_cast<int64_t>(code) * code;
      }
      norm_squared_[r] = norm;
    }
    valid_ = true;
    return true;
  }

  void reset()
  {
    valid_ = false;
    kernel_ = kernel_t::reference;
    row_count_ = 0;
    dimension_ = 0;
    codes_.clear();
    biased_.clear();
    norm_squared_.clear();
  }

  bool valid() const { return valid_; }
  kernel_t kernel() const { return kernel_; }
  size_t row_count() const { return row_count_; }
  uint32_t dimension() const { return dimension_; }

  const int16_t *codes(size_t row) const
  {
    return codes_.data() + row * dimension_;
  }
  const uint8_t *biased(size_t row) const
  {
    return biased_.data() + row * dimension_;
  }
  int64_t norm_squared(size_t row) const { return norm_squared_[row]; }

private:
  bool valid_ = false;
  kernel_t kernel_ = kernel_t::reference;
  size_t row_count_ = 0;
  uint32_t dimension_ = 0;
  std::vector<int16_t> codes_;
  std::vector<uint8_t> biased_;
  std::vector<int64_t> norm_squared_;
};

// ---- portable integer kernels ----------------------------------------
//
// Used for the tails of the vector kernels, and as the whole kernel when
// a build has no x86 SIMD but a caller has still asked for codes.

inline candidate_stats_t candidate_stats_scalar(const char *candidate_bytes,
                                                uint32_t dimension)
{
  candidate_stats_t stats;
  int64_t norm = 0;
  int32_t sum = 0;
  for (uint32_t i = 0; i < dimension; ++i) {
    int8_t code = 0;
    std::memcpy(&code, candidate_bytes + i, sizeof(code));
    norm += static_cast<int64_t>(code) * code;
    sum += code;
  }
  stats.norm_squared = norm;
  stats.code_sum = sum;
  return stats;
}

inline int64_t dot_scalar(const int16_t *query_codes,
                          const char *candidate_bytes,
                          uint32_t dimension)
{
  int64_t dot = 0;
  for (uint32_t i = 0; i < dimension; ++i) {
    int8_t code = 0;
    std::memcpy(&code, candidate_bytes + i, sizeof(code));
    dot += static_cast<int64_t>(query_codes[i]) * code;
  }
  return dot;
}

#if defined(CEPH_VECTOR_INT8_SIMD_X86)

// ---- AVX2 integer kernels --------------------------------------------

__attribute__((__target__("avx2")))
inline int32_t horizontal_sum_epi32(__m256i acc)
{
  alignas(32) int32_t lanes[8];
  _mm256_store_si256(reinterpret_cast<__m256i*>(lanes), acc);
  return lanes[0] + lanes[1] + lanes[2] + lanes[3] +
         lanes[4] + lanes[5] + lanes[6] + lanes[7];
}

// Sixteen signed codes widened to int16. _mm_loadu_si128 reads exactly the
// sixteen bytes the caller has guaranteed are in range; the loop that uses
// it stops sixteen bytes short of the end and the tail is scalar, so this
// never over-reads a record.
__attribute__((__target__("avx2")))
inline __m256i load_int8x16_epi16(const char *bytes, uint32_t i)
{
  return _mm256_cvtepi8_epi16(
      _mm_loadu_si128(reinterpret_cast<const __m128i*>(bytes + i)));
}

// ||v||^2 and sum(v) in one pass. madd_epi16 against a vector of ones is
// the cheapest exact way to widen-and-sum int16 lanes to int32.
__attribute__((__target__("avx2")))
inline candidate_stats_t candidate_stats_avx2(const char *candidate_bytes,
                                              uint32_t dimension)
{
  const __m256i ones = _mm256_set1_epi16(1);
  __m256i norm_acc = _mm256_setzero_si256();
  __m256i sum_acc = _mm256_setzero_si256();

  uint32_t i = 0;
  for (; i + 16 <= dimension; i += 16) {
    const __m256i v = load_int8x16_epi16(candidate_bytes, i);
    norm_acc = _mm256_add_epi32(norm_acc, _mm256_madd_epi16(v, v));
    sum_acc = _mm256_add_epi32(sum_acc, _mm256_madd_epi16(v, ones));
  }

  candidate_stats_t stats;
  stats.norm_squared = horizontal_sum_epi32(norm_acc);
  stats.code_sum = horizontal_sum_epi32(sum_acc);

  for (; i < dimension; ++i) {
    int8_t code = 0;
    std::memcpy(&code, candidate_bytes + i, sizeof(code));
    stats.norm_squared += static_cast<int64_t>(code) * code;
    stats.code_sum += code;
  }
  return stats;
}

// Four query rows per candidate load, the same shape as the float32
// batched kernel -- but sixteen dimensions per step instead of four, and
// the candidate is widened once and reused by all four rows instead of
// being re-decoded per row.
__attribute__((__target__("avx2")))
inline void dots_avx2(const query_codes_t& queries,
                      size_t row_count,
                      const char *candidate_bytes,
                      uint32_t dimension,
                      int64_t *dots)
{
  size_t q = 0;
  for (; q + 4 <= row_count; q += 4) {
    const int16_t *q0 = queries.codes(q + 0);
    const int16_t *q1 = queries.codes(q + 1);
    const int16_t *q2 = queries.codes(q + 2);
    const int16_t *q3 = queries.codes(q + 3);
    __m256i a0 = _mm256_setzero_si256();
    __m256i a1 = _mm256_setzero_si256();
    __m256i a2 = _mm256_setzero_si256();
    __m256i a3 = _mm256_setzero_si256();
    uint32_t i = 0;
    for (; i + 16 <= dimension; i += 16) {
      const __m256i c = load_int8x16_epi16(candidate_bytes, i);
      a0 = _mm256_add_epi32(a0, _mm256_madd_epi16(
          _mm256_loadu_si256(reinterpret_cast<const __m256i*>(q0 + i)), c));
      a1 = _mm256_add_epi32(a1, _mm256_madd_epi16(
          _mm256_loadu_si256(reinterpret_cast<const __m256i*>(q1 + i)), c));
      a2 = _mm256_add_epi32(a2, _mm256_madd_epi16(
          _mm256_loadu_si256(reinterpret_cast<const __m256i*>(q2 + i)), c));
      a3 = _mm256_add_epi32(a3, _mm256_madd_epi16(
          _mm256_loadu_si256(reinterpret_cast<const __m256i*>(q3 + i)), c));
    }
    dots[q + 0] = horizontal_sum_epi32(a0);
    dots[q + 1] = horizontal_sum_epi32(a1);
    dots[q + 2] = horizontal_sum_epi32(a2);
    dots[q + 3] = horizontal_sum_epi32(a3);
    if (i < dimension) {
      const uint32_t rest = dimension - i;
      dots[q + 0] += dot_scalar(q0 + i, candidate_bytes + i, rest);
      dots[q + 1] += dot_scalar(q1 + i, candidate_bytes + i, rest);
      dots[q + 2] += dot_scalar(q2 + i, candidate_bytes + i, rest);
      dots[q + 3] += dot_scalar(q3 + i, candidate_bytes + i, rest);
    }
  }
  for (; q < row_count; ++q) {
    const int16_t *query = queries.codes(q);
    __m256i acc = _mm256_setzero_si256();
    uint32_t i = 0;
    for (; i + 16 <= dimension; i += 16) {
      const __m256i c = load_int8x16_epi16(candidate_bytes, i);
      acc = _mm256_add_epi32(acc, _mm256_madd_epi16(
          _mm256_loadu_si256(
            reinterpret_cast<const __m256i*>(query + i)), c));
    }
    dots[q] = horizontal_sum_epi32(acc);
    if (i < dimension) {
      dots[q] += dot_scalar(query + i, candidate_bytes + i, dimension - i);
    }
  }
}

// ---- AVX512-VNNI kernels ---------------------------------------------
//
// VPDPBUSD does a four-deep u8 x s8 dot product per int32 lane in one
// instruction, so one step covers thirty-two dimensions -- twice the AVX2
// kernel's sixteen. It buys that width by taking its first operand as
// UNSIGNED, which is where every correction below comes from.

// v + 128 as unsigned bytes, which in two's complement is just the sign
// bit flipped. One XOR, so making the candidate the unsigned operand
// costs nothing -- used by candidate_stats_vnni() below, where the
// candidate has to appear on both sides of a product.
__attribute__((__target__("avx512vnni,avx512vl")))
inline __m256i bias_to_unsigned(__m256i signed_bytes)
{
  return _mm256_xor_si256(signed_bytes, _mm256_set1_epi8(int8_t(0x80)));
}

__attribute__((__target__("avx512vnni,avx512vl")))
inline int64_t horizontal_sum_epi32_vnni(__m256i acc)
{
  alignas(32) int32_t lanes[8];
  _mm256_store_si256(reinterpret_cast<__m256i*>(lanes), acc);
  return int64_t(lanes[0]) + lanes[1] + lanes[2] + lanes[3] +
         int64_t(lanes[4]) + lanes[5] + lanes[6] + lanes[7];
}

// ||v||^2 and sum(v) at the VNNI step width. Squaring a signed byte is
// not something VPDPBUSD can do directly, so the candidate is biased into
// the unsigned operand and the bias taken back out afterwards:
//
//   sum_i (v_i + 128) * v_i = ||v||^2 + 128 * sum(v)
//
// sum(v) itself comes from the same instruction against a vector of ones.
__attribute__((__target__("avx512vnni,avx512vl")))
inline candidate_stats_t candidate_stats_vnni(const char *candidate_bytes,
                                              uint32_t dimension)
{
  const __m256i ones = _mm256_set1_epi8(1);
  __m256i sum_acc = _mm256_setzero_si256();
  __m256i biased_norm_acc = _mm256_setzero_si256();

  uint32_t i = 0;
  for (; i + 32 <= dimension; i += 32) {
    const __m256i v =
      _mm256_loadu_si256(reinterpret_cast<const __m256i*>(candidate_bytes + i));
    sum_acc = _mm256_dpbusd_epi32(sum_acc, ones, v);
    biased_norm_acc = _mm256_dpbusd_epi32(
        biased_norm_acc, bias_to_unsigned(v), v);
  }

  const int64_t prefix_sum = horizontal_sum_epi32_vnni(sum_acc);
  candidate_stats_t stats;
  stats.norm_squared =
    horizontal_sum_epi32_vnni(biased_norm_acc) - int64_t(128) * prefix_sum;
  stats.code_sum = static_cast<int32_t>(prefix_sum);

  for (; i < dimension; ++i) {
    int8_t code = 0;
    std::memcpy(&code, candidate_bytes + i, sizeof(code));
    stats.norm_squared += static_cast<int64_t>(code) * code;
    stats.code_sum += code;
  }
  return stats;
}

// sum(v) over the 32-aligned prefix alone. The bias correction has to
// match exactly the elements VPDPBUSD covered, and the scalar tail below
// is computed from unbiased codes, so the tail's contribution to sum(v)
// is removed here rather than added twice.
inline int64_t vectorized_prefix_code_sum(const candidate_stats_t& stats,
                                          const char *candidate_bytes,
                                          uint32_t dimension,
                                          uint32_t consumed)
{
  int64_t prefix = stats.code_sum;
  for (uint32_t i = consumed; i < dimension; ++i) {
    int8_t code = 0;
    std::memcpy(&code, candidate_bytes + i, sizeof(code));
    prefix -= code;
  }
  return prefix;
}

// Four query rows per candidate load, the same blocking as the AVX2
// kernel. Without it VPDPBUSD's extra width is spent re-reading the
// candidate once per query and the wider instruction loses to AVX2 from
// four queries up.
__attribute__((__target__("avx512vnni,avx512vl")))
inline void dots_vnni(const query_codes_t& queries,
                      size_t row_count,
                      const char *candidate_bytes,
                      uint32_t dimension,
                      const candidate_stats_t& stats,
                      int64_t *dots)
{
  const uint32_t consumed = dimension & ~uint32_t(31);
  // sum_i (q_i + 128) v_i = q.v + 128 sum(v), so every dot below comes out
  // high by the same per-candidate amount. Computed once for the batch.
  const int64_t bias =
    int64_t(128) * vectorized_prefix_code_sum(
        stats, candidate_bytes, dimension, consumed);
  const uint32_t tail = dimension - consumed;

  size_t q = 0;
  for (; q + 4 <= row_count; q += 4) {
    const uint8_t *q0 = queries.biased(q + 0);
    const uint8_t *q1 = queries.biased(q + 1);
    const uint8_t *q2 = queries.biased(q + 2);
    const uint8_t *q3 = queries.biased(q + 3);
    __m256i a0 = _mm256_setzero_si256();
    __m256i a1 = _mm256_setzero_si256();
    __m256i a2 = _mm256_setzero_si256();
    __m256i a3 = _mm256_setzero_si256();
    for (uint32_t i = 0; i < consumed; i += 32) {
      const __m256i c = _mm256_loadu_si256(
          reinterpret_cast<const __m256i*>(candidate_bytes + i));
      a0 = _mm256_dpbusd_epi32(a0, _mm256_loadu_si256(
          reinterpret_cast<const __m256i*>(q0 + i)), c);
      a1 = _mm256_dpbusd_epi32(a1, _mm256_loadu_si256(
          reinterpret_cast<const __m256i*>(q1 + i)), c);
      a2 = _mm256_dpbusd_epi32(a2, _mm256_loadu_si256(
          reinterpret_cast<const __m256i*>(q2 + i)), c);
      a3 = _mm256_dpbusd_epi32(a3, _mm256_loadu_si256(
          reinterpret_cast<const __m256i*>(q3 + i)), c);
    }
    dots[q + 0] = horizontal_sum_epi32_vnni(a0) - bias;
    dots[q + 1] = horizontal_sum_epi32_vnni(a1) - bias;
    dots[q + 2] = horizontal_sum_epi32_vnni(a2) - bias;
    dots[q + 3] = horizontal_sum_epi32_vnni(a3) - bias;
    if (tail != 0) {
      for (size_t row = q; row < q + 4; ++row) {
        dots[row] += dot_scalar(queries.codes(row) + consumed,
                                candidate_bytes + consumed, tail);
      }
    }
  }
  for (; q < row_count; ++q) {
    const uint8_t *query = queries.biased(q);
    __m256i acc = _mm256_setzero_si256();
    for (uint32_t i = 0; i < consumed; i += 32) {
      const __m256i c = _mm256_loadu_si256(
          reinterpret_cast<const __m256i*>(candidate_bytes + i));
      acc = _mm256_dpbusd_epi32(acc, _mm256_loadu_si256(
          reinterpret_cast<const __m256i*>(query + i)), c);
    }
    dots[q] = horizontal_sum_epi32_vnni(acc) - bias;
    if (tail != 0) {
      dots[q] += dot_scalar(queries.codes(q) + consumed,
                            candidate_bytes + consumed, tail);
    }
  }
}

#endif // CEPH_VECTOR_INT8_SIMD_X86

// ---- dispatch --------------------------------------------------------

inline candidate_stats_t candidate_stats(kernel_t kernel,
                                         const char *candidate_bytes,
                                         uint32_t dimension)
{
#if defined(CEPH_VECTOR_INT8_SIMD_X86)
  // Both fast paths need ||v||^2 and sum(v) per candidate; each computes
  // them at its own step width.
  if (kernel == kernel_t::vnni) {
    return candidate_stats_vnni(candidate_bytes, dimension);
  }
  if (kernel == kernel_t::avx2) {
    return candidate_stats_avx2(candidate_bytes, dimension);
  }
#else
  (void)kernel;
#endif
  return candidate_stats_scalar(candidate_bytes, dimension);
}

// Dot products of one candidate against every prepared query row. `dots`
// holds row_count int64s. Exact: no rounding happens anywhere in here.
inline void dots(const query_codes_t& queries,
                 size_t row_count,
                 const char *candidate_bytes,
                 uint32_t dimension,
                 const candidate_stats_t& stats,
                 int64_t *out_dots)
{
#if defined(CEPH_VECTOR_INT8_SIMD_X86)
  switch (queries.kernel()) {
  case kernel_t::vnni:
    dots_vnni(queries, row_count, candidate_bytes, dimension, stats,
              out_dots);
    return;
  case kernel_t::avx2:
    dots_avx2(queries, row_count, candidate_bytes, dimension, out_dots);
    return;
  case kernel_t::reference:
    break;
  }
#else
  (void)stats;
#endif
  for (size_t q = 0; q < row_count; ++q) {
    out_dots[q] = dot_scalar(queries.codes(q), candidate_bytes, dimension);
  }
}

// One prepared query against one candidate, as the full triple. The
// caller builds a one-row query_codes_t (the single-query path has one
// query by construction).
//
// sum_sq is derived rather than accumulated: ||q-v||^2 = ||q||^2 +
// ||v||^2 - 2 q.v is an identity over integers, so unlike the float32
// batched kernel this derivation is not a rounding trade -- it reproduces
// the reference kernel's euclidean distance bit for bit.
inline terms_t single_terms(const query_codes_t& queries,
                            const char *candidate_bytes,
                            uint32_t dimension)
{
  const candidate_stats_t stats =
    candidate_stats(queries.kernel(), candidate_bytes, dimension);
  int64_t dot = 0;
  dots(queries, 1, candidate_bytes, dimension, stats, &dot);

  terms_t terms;
  terms.dot = dot;
  terms.candidate_norm_squared = stats.norm_squared;
  terms.sum_sq = queries.norm_squared(0) + stats.norm_squared - 2 * dot;
  return terms;
}

} // namespace int8_simd
} // namespace vector_query_exec
} // namespace rados
} // namespace ceph

#endif // CEPH_COMMON_VECTOR_QUERY_EXEC_INT8_SIMD_H
