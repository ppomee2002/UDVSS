// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <getopt.h>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <numeric>
#include <optional>
#include <random>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "common/vector_record.h"
#include "common/vector_pivot_tree.h"
#include "common/vector_selective_fanout.h"
#include "include/buffer.h"
#include "include/rados/librados.hpp"
#include "include/rados/vector_ops.h"
#include "librados/vector_pg_lsh.h"

namespace {

using clock_type = std::chrono::steady_clock;
namespace pg_lsh = librados::vector_pg_lsh;
namespace sf = ceph::rados::vector_selective_fanout;

struct options_t {
  std::string base_path;
  std::string query_path;
  std::string groundtruth_path;
  std::string pool;
  std::string bucket = "siftsmall";
  std::string index = "fixed-layout";
  std::string key_prefix = "siftsmall-";
  std::string client_name = "client.admin";
  std::string cluster_name = "ceph";
  std::string conf_path;
  std::string operation = "both";
  std::string routing_mode;
  // "float32" (default) or "int8": store and query vectors scalar-
  // quantized with one global symmetric scale.
  std::string quantize = "float32";
  // Derived from --base once the dataset is read, not parsed: the scale
  // the int8 codes are expressed in. 1.0 when --quantize is float32.
  float quantize_scale = 1.0f;
  uint32_t pg_num = 8;
  // Fixed partition space (index_config_t::partition_num); 0 keeps the
  // original layout. --pg-num is then only the resolution queries route
  // at and may be changed on a loaded index, e.g. after a PG split.
  uint32_t partition_num = 0;
  // One locator per LSH bucket; a bucket's PG is wherever RADOS maps that
  // locator at the current pg_num (--pg-num must be the pool's current
  // pg_num). Nothing is pinned at load time, so the same index is queried
  // the same way after any native split or merge.
  // 1 (--bucket-locators): the locator is a plain per-bucket string.
  // 2 (--lsh-aligned-locators): the locator is salted so the PG is the low
  // log2(pg_num) bits of the LSH bucket id (see index_config_t).
  uint32_t bucket_locators = 0;
  // index_config_t::placement_mode: fixed_lsh (default),
  // hierarchical_lsh, with its persisted root scale S (the hierarchy is
  // rooted at the index's anchor as built below, unchanged), or
  // training_derived, whose per-table trees are trained on the
  // --pivot-build-sample rows of --base (see requested_config()).
  uint32_t placement_mode = pg_lsh::placement_mode_fixed_lsh;
  double hierarchical_scale = 0;
  // Exact per-PG search: every probed PG is answered by scanning every
  // vector object --object-list places in it now (no tree search), so the
  // result depends only on which vectors each PG holds.
  bool full_pg_scan = false;
  uint32_t k = 10;
  uint32_t l = 16;
  uint32_t seed = 1315423911U;
  uint32_t d = 1;
  // Load path only, and a post-filter on the already-built target list
  // rather than a change to any routing function. Both routings build
  // their write targets as a prefix of a d-independent ranking, so
  // targets(d=D) is targets(d=D-1) plus one more entry. Writing only ranks
  // [begin, d) therefore reproduces exactly the store state a full --d D
  // load would have produced, given ranks [0, begin) are already present
  // from an earlier load -- which is how a d=1 index is grown to d=2/d=3
  // without re-PUTting the copies it already holds.
  uint32_t write_rank_begin = 0;
  uint32_t m = 1;
  uint32_t hamming_radius = 0;
  uint32_t distance_bucket_bits = 0;
  uint32_t residual_bits = 0;
  uint32_t distance_bucket_radius = 0;
  uint32_t residual_hamming_radius = 0;
  uint32_t probe_limit_per_pg = 0;
  // pg-suboid only: disables the fixed client-side distance_bucket/
  // residual Hamming-ball probe expansion (distance_bucket_radius/
  // residual_hamming_radius must both be 0 in this mode) and lets the OSD
  // expand neighboring sub_oid ONodes itself -- see
  // librados::vector_pg_lsh::query_params_t::server_side_boundary_search.
  bool server_side_boundary_search = false;
  // pg-suboid only: vector-aware multi-path *tree* traversal (see
  // librados::vector_pg_lsh::query_params_t::tree_boundary_search_budget).
  // A distinct, independent mechanism from server_side_boundary_search
  // above -- never both set. 0 (the default) disables it.
  uint32_t tree_search_budget = 0;
  // Uses index layout pg_lsh_layout_version_raw_euclidean_v1: distance
  // buckets are raw Euclidean distances from a data centroid, the same
  // metric space as top-k tau. Required by --tree-prune, whose subtree
  // exclusion is only safe under this layout.
  bool raw_euclidean_geometry = false;
  // Tree-search-budget only: opt-in real tau-based subtree EXCLUSION
  // (librados::vector_pg_lsh::query_params_t::tree_boundary_search_prune)
  // instead of the existing best-first-reordering-only behavior. Requires
  // --raw-euclidean-geometry and --tree-search-budget > 0.
  bool tree_prune = false;
  // fanout-query only: query_vectors_request_t::reply_flags bits.
  bool reply_include_vector = false;
  bool reply_omit_tree_stats = false;
  // fanout-query only, --query-concurrency 1: shell command run as
  // "<hook> begin" right before the measured run's first query (after the
  // locator precompute) and "<hook> end" right after its last one, so an
  // external byte counter can bracket exactly the measured queries.
  std::string measure_hook;
  // fanout-query only: when > 0, the warmup runs exactly this many queries
  // (cycling the query set) instead of --warmup-rounds full passes.
  uint64_t warmup_queries = 0;
  // Tree-search-budget only: how many consecutive expansions that fail to
  // narrow tau a probe may make before it stops
  // (query_params_t::tree_boundary_search_no_improve_patience). 0 keeps the
  // existing stop-on-first-non-improving-expansion behaviour.
  uint32_t tree_no_improve_patience = 0;
  // Tree-search-budget only: cap on ONodes the OSD may open per probe
  // (query_params_t::tree_boundary_search_onode_budget). 0 is unlimited.
  uint32_t tree_onode_budget = 0;
  // routing-mode=pivot only: see build_pivot_model()/pivot_rank_pgs() below.
  // write_top_pgs/probe_pgs reuse --d/--m (same "how many PGs" meaning as
  // LSH's write/query fanout).
  uint32_t pivot_build_sample = 16384;
  uint32_t pivot_probe_budget = 12;
  // Lloyd (k-means) refinement passes applied to the farthest-first
  // representatives. 0 keeps the farthest-first model exactly as-is.
  uint32_t pivot_refine_iters = 0;
  // Spherical Lloyd: renormalise every refined representative to the mean
  // norm of the build sample (1.0 for unit-norm data). Only affects the
  // refinement, so it changes nothing when pivot_refine_iters is 0.
  bool pivot_spherical = false;
  // Balanced Lloyd: cap each cluster at ceil((1+eps) * samples / pg_num)
  // during refinement. 0 = plain Lloyd.
  double pivot_balance_eps = 0.0;
  // route-sim only (not wired into load/query yet):
  //  place-cap-eps   cap each PG at ceil((1+eps) * N / pg_num) vectors when
  //                  placing --base (0 = nearest PG, as load_vectors does)
  //  radius-alpha/q  query score |q-c_p| - alpha * r_p, r_p = quantile q of
  //                  member-to-rep distance over the PG's sampled members
  //  multi-rep R     query score min over R sub-representatives per PG,
  //                  k-means'd from the PG's sampled members
  double sim_place_cap_eps = 0.0;
  double sim_radius_alpha = 0.0;
  double sim_radius_q = 0.9;
  uint32_t sim_multi_rep = 1;
  // operation=route-sim only: the M values to report (comma separated).
  std::string sim_m_list = "1,2,4,8,16,32,64,96";
  uint32_t top_k = 10;
  // SQ8 ranks by quantized distance, so its ordering differs from
  // float32's even when the same vectors are reachable. --rerank-top-n N
  // asks the OSD for an N-deep shortlist instead of top_k, then rescores
  // that shortlist on the client with the ORIGINAL float32 vectors and
  // keeps top_k. 0 disables it and every code path stays as it was.
  uint32_t rerank_top_n = 0;
  // Built once when rerank is on; null otherwise. shared_ptr because
  // options_t is copied into the per-operation config.
  std::shared_ptr<const struct rerank_context_t> rerank;
  uint32_t query_concurrency = 1;
  uint32_t load_concurrency = 1;
  uint32_t warmup_rounds = 1;
  uint32_t rounds = 1;
  uint64_t min_queries = 0;
  uint32_t min_seconds = 0;
  size_t base_limit = 0;
  size_t query_limit = 0;
  std::string query_index_list_path;
  std::string destination_pg_log_path;
  // fanout-query only: "sequential" or "parallel" dispatch of the m probes
  // a single logical query resolves to.
  std::string fanout_mode = "parallel";
  // fanout-query only: RADOS ops kept outstanding per --query-concurrency
  // worker. 0 submits a whole stage and then waits on it, one logical query
  // at a time; N > 0 keeps a rolling window of N probe slots refilled as
  // each completes, filled across several in-flight logical queries. A
  // client-side queue depth, unrelated to seastore's io_concurrency.
  uint32_t io_depth = 0;
  // fanout-query only: how many logical queries a worker resolves before
  // dispatching, so that probes landing on the SAME object can share one
  // RADOS request. 1 (the default) keeps the existing one-request-per-probe
  // dispatcher untouched.
  //
  // The grouping key is the probe's object, not its PG. Under
  // --routing-mode pivot/pg-container those are the same thing (one object
  // per PG, empty sub_oid), which is the case this option exists for. Under
  // pg-suboid each query resolves to its own anchor sub_oid object, so
  // queries only share a request when their anchors happen to collide -- a
  // RADOS op vector addresses exactly one object and this option does not
  // change that.
  uint32_t query_batch_size = 1;
  // fanout-query only: dump every completed logical query's merged top-k as
  // query_index,rank,entry_id,distance. Exists so two runs can be compared
  // on the actual result rows rather than on an aggregate recall number.
  std::string query_results_csv_path;
  std::string fanout_log_path;
  uint64_t fanout_log_count = 10;
  // fanout-query only: dispatch the m routed PGs in growing cumulative
  // batches instead of all at once, re-examining the merged global top-k
  // between batches (see common/vector_selective_fanout.h).
  // Query-side probe ordering (see librados/vector_placement.h's
  // pg_lsh_v0_query_routing_t). Neither changes placement.
  uint32_t query_table_count = 0;
  bool margin_ordered_probes = false;
  bool selective = false;
  std::string selective_schedule = "8,16,32,64,96";
  // "never" (default) runs every stage of the schedule for every query,
  // which is what a telemetry run wants: one CSV row per (query, stage) so
  // stopping rules can be compared offline against per-stage recall.
  // "tau-relative" replays the candidate rule live.
  std::string selective_stop = "never";
  double selective_min_improvement = 0.02;
  std::string selective_csv_path;
  // object-census only: `rados ls` output naming the objects to census.
  std::string object_list_path;
  // placement-audit only: the pool pg_num to audit placement against.
  uint32_t audit_pg_num = 0;
};

struct float_dataset_t {
  uint32_t dimension = 0;
  std::vector<std::vector<float>> rows;
};

struct ivec_dataset_t {
  std::vector<std::vector<uint32_t>> rows;
};

struct load_result_t {
  uint64_t completed = 0;
  uint64_t failures = 0;
  uint64_t put_targets = 0;
  // Rows whose target list --write-rank-begin consumed whole: the routing
  // produced fewer than begin+1 unique PGs for this vector, so a full load
  // would also have written it fewer times and there is no rank-`begin`
  // copy to add. Skipping it is what keeps the two equivalent.
  uint64_t rank_skipped = 0;
  uint64_t target_violations = 0;
  int first_error = 0;
  double seconds = 0;
  std::set<std::string> vector_oids;
  std::string first_oid;
};

struct query_result_t {
  uint64_t completed = 0;
  uint64_t failures = 0;
  uint64_t missing_objects = 0;
  uint64_t empty_results = 0;
  uint64_t unsorted_results = 0;
  uint64_t zero_counter_results = 0;
  uint64_t pg_probes = 0;
  uint64_t object_probes = 0;
  uint64_t matching_records = 0;
  uint64_t distance_computations = 0;
  uint64_t returned_entries = 0;
  uint64_t recall_hits = 0;
  uint64_t recall_denominator = 0;
  // Shortlist entries whose base row could not be recovered, so they
  // kept their server-side rank instead of being rescored. Expected 0.
  uint64_t rerank_unmapped = 0;
  int first_error = 0;
  double seconds = 0;
  std::vector<double> latency_us;
  std::string first_oid;
};

void usage(std::ostream& out, const char *program)
{
  out << "Usage: " << program << " --routing-mode MODE --pool POOL [options]\n"
      << "\n"
      << "Native VectorNode PG-LSH benchmark. MODE is pg-container or pg-suboid.\n"
      << "\n"
      << "Pivot routing (--routing-mode pivot):\n"
      << "  --pivot-build-sample N --pivot-probe-budget N\n"
      << "  --pivot-refine-iters N: after farthest-first selection, run N\n"
      << "    Lloyd (k-means) passes over the build sample -- reassign every\n"
      << "    sample to its nearest representative, then move each\n"
      << "    representative to the mean of its cluster. Default 0 (the\n"
      << "    unrefined farthest-first model). Empty clusters keep their\n"
      << "    previous representative. The model is never persisted, so a\n"
      << "    query run MUST pass the same value as the load run that filled\n"
      << "    the index -- check the PIVOT_MODEL digest, a mismatch shows up\n"
      << "    only as collapsed recall.\n"
      << "  --pivot-spherical: renormalise refined representatives to the\n"
      << "    sample's mean norm after each Lloyd pass (spherical k-means).\n"
      << "  --pivot-balance-eps E: balanced Lloyd, each cluster capped at\n"
      << "    ceil((1+E) * samples / pg_num). 0 (default) = plain Lloyd.\n"
      << "Offline routing simulation (--operation route-sim, pivot only):\n"
      << "  needs no cluster. Places every --base row in its nearest PG and\n"
      << "  reports, per M in --sim-m-list, the recall@top-k and candidate\n"
      << "  count a full scan of the M nearest PGs would give.\n"
      << "  --pivot-place-cap-eps E (alias --sim-place-cap-eps): capped\n"
      << "    placement, each PG capped at ceil((1+E) * N / pg_num) vectors;\n"
      << "    honoured by load (d must be 1) and route-sim. Queries are\n"
      << "    unaffected -- they still rank PGs by representative distance.\n"
      << "  --sim-radius-alpha A --sim-radius-q Q: radius-aware query ranking\n"
      << "  --sim-multi-rep R: R sub-representatives per PG for query ranking\n"
      << "Dataset: --base FILE --query FILE --groundtruth FILE\n"
      << "Operation: --operation load|query|both|fanout-query|route-sim"
      << " --base-limit N --query-limit N\n"
      << "  --query-index-list FILE: use exactly these query-file row indices,\n"
      << "    in this order, instead of the first --query-limit rows.\n"
      << "  --destination-pg-log FILE: during the measured query run, record\n"
      << "    ticket,query_index,pg for the first requests (debug/verification).\n"
      << "  fanout-query: one logical query resolves (via --m) to m distinct\n"
      << "    candidate PGs, all queried for the same query vector; results are\n"
      << "    merged (dedup by key + global top-k) into one logical result.\n"
      << "    --fanout-mode sequential|parallel (default parallel)\n"
      << "    --io-depth N: keep N RADOS ops outstanding per worker,\n"
      << "      refilling each slot as it completes, instead of submitting\n"
      << "      a whole stage and waiting on it (default 0).\n"
      << "    --query-batch-size N: resolve N logical queries per worker\n"
      << "      before dispatching and pack the probes that land on the same\n"
      << "      object into ONE RADOS request (N QUERY_VECTORS ops in one\n"
      << "      ObjectOperation). Routing, fan-out and the per-query merge\n"
      << "      are unchanged; only the number of requests on the wire\n"
      << "      drops. Needs --fanout-mode parallel, and is incompatible\n"
      << "      with --selective and --io-depth (default 1).\n"
      << "    --query-results-csv FILE: one row per (query, rank) with\n"
      << "      entry_id and distance, for diffing two runs' actual top-k\n"
      << "      instead of just their recall.\n"
      << "    --fanout-log FILE --fanout-log-count N: dump the first N logical\n"
      << "      queries' per-subrequest pg/sub_oid/submit_us/complete_us.\n"
      << "    --selective: dispatch the m routed PGs in growing cumulative\n"
      << "      batches, re-examining the merged global top-k between them,\n"
      << "      instead of sending all m at once. Slices ONE routing result\n"
      << "      per query -- no PG is re-routed or re-queried.\n"
      << "      --selective-schedule 8,16,32,64,96 (cumulative; last must\n"
      << "        equal --m)\n"
      << "      --selective-stop never|tau-relative (default never: run every\n"
      << "        stage for every query, which is what --selective-csv wants)\n"
      << "      --selective-min-improvement F (tau-relative only, default 0.02)\n"
      << "      --selective-csv FILE: one row per (query, stage) with d1, tau,\n"
      << "        tau/d1, tau improvement, top-k churn, candidates, latency and\n"
      << "        ground-truth recall, for comparing stopping rules offline.\n"
      << "  --rerank-top-n N\n"
      << "      Ask the OSD for an N-deep shortlist instead of --top-k, then\n"
      << "      rescore it on the client against the original float32 vectors\n"
      << "      and keep --top-k. Recovers the ordering SQ8 quantization\n"
      << "      loses; cannot recover what routing never reached, so its\n"
      << "      ceiling is the float32 recall at the same --m. Needs --base.\n"
      << "Routing: --pg-num N --k N --l N --seed N --d N --m N\n"
      << "  --partition-num N: pg-suboid/pg-container only. Fix the index's\n"
      << "    partition space at N (a power of two >= --pg-num). Vectors\n"
      << "    are written to N partitions whose locators never change, so\n"
      << "    the pool's pg_num can grow up to N without a rewrite. --pg-num\n"
      << "    is then the resolution queries route at; each routed PG is\n"
      << "    probed as its N / --pg-num partitions.\n"
      << "  --bucket-locators: one locator per LSH bucket, routed at the\n"
      << "    pool's current pg_num (--pg-num); survives native split/merge.\n"
      << "  --lsh-aligned-locators: like --bucket-locators, but each locator\n"
      << "    is salted so the PG is the low log2(pg_num) bits of the LSH\n"
      << "    bucket id: a split uses one more signature bit, a merge one\n"
      << "    less. Needs a power-of-two pg_num <= 2^k, --d 1 and\n"
      << "    --query-table-count 1 on queries.\n"
      << "  --placement-mode fixed_lsh|hierarchical_lsh|training_derived:\n"
      << "    how the k-bit LSH bucket id is derived (default fixed_lsh).\n"
      << "    hierarchical_lsh needs --hierarchical-scale S > 0 (pg-suboid\n"
      << "    only). training_derived (pg-suboid only) trains, per table, a\n"
      << "    pivot p_R and distance boundary r_R for every region on the\n"
      << "    --pivot-build-sample rows of --base; b_R = d(v,p_R) < r_R ? 0\n"
      << "    : 1, b_1 at the LSB. Objects, locators, multi-probe and\n"
      << "    split/merge are the hierarchical_lsh ones. The trees are not\n"
      << "    persisted: query runs must pass the same --base/--base-limit/\n"
      << "    --pivot-build-sample/--k/--l/--seed (check TD_MODEL digest).\n"
      << "  --hierarchical-scale S: root offset scale; level l moves the\n"
      << "    child pivot by S / 2^(l+1). Persisted in the index.\n"
      << "         --hamming-radius N --distance-bucket-bits N --residual-bits N\n"
      << "         --distance-bucket-radius N --residual-hamming-radius N\n"
      << "         --probe-limit-per-pg N\n"
      << "  --query-table-count N: probe only the first N of the L hash\n"
      << "    tables (0 = all, the default). Query-side only -- placement is\n"
      << "    untouched, so this is safe on an already-loaded index. Data\n"
      << "    written with --d D only ever lands in PGs derived from tables\n"
      << "    [0,D), so probing beyond that spends budget on PGs that cannot\n"
      << "    hold a related vector. Set this to --d.\n"
      << "  --margin-ordered-probes: order perturbations by ascending\n"
      << "    sum-of-|projection| over the bits they flip (query-directed\n"
      << "    multi-probe LSH) instead of by hamming distance then bit index.\n"
      << "  --raw-euclidean-geometry: pg-suboid only, requires\n"
      << "    --distance-bucket-bits > 0. Places sub_oid distance_bucket by\n"
      << "    RAW (non-unit-normalized) Euclidean distance from a\n"
      << "    data-centroid anchor instead of v0's normalized angular\n"
      << "    distance, in the same metric space as top-k tau -- see\n"
      << "    librados/vector_pg_lsh.h's pg_lsh_layout_version_raw_\n"
      << "    euclidean_v1. Required for --tree-prune.\n"
      << "  --tree-search-budget N --tree-prune: real tau-based subtree\n"
      << "    EXCLUSION (not just best-first reordering) within the tree\n"
      << "    search; requires --raw-euclidean-geometry.\n"
      << "  --tree-search-budget N --tree-no-improve-patience N: let a probe\n"
      << "    survive N consecutive expansions that do not narrow tau\n"
      << "    (default 0: stop at the first one).\n"
      << "  --tree-search-budget N --tree-onode-budget N: cap the ONodes the\n"
      << "    OSD opens per probe (default 0: unlimited).\n"
      << "Load: --load-concurrency N --write-rank-begin R\n"
      << "    --write-rank-begin R writes only write-target ranks\n"
      << "    [R, --d), growing an index loaded at --d R to --d D\n"
      << "    without re-PUTting the copies it already holds.\n"
      << "Query: --top-k N --query-concurrency N --warmup-rounds N --rounds N\n"
      << "       --min-queries N --min-seconds N\n"
      << "Ceph: --conf FILE --client NAME --cluster NAME\n"
      << "Operations: load|query|both|fanout-query|object-census|\n"
      << "            placement-audit\n"
      << "  object-census --object-list FILE: scan every listed vector\n"
      << "    object (a `rados ls` dump) through its partition's locator and\n"
      << "    print oid,partition,sub_oid,phys_pg,r,op_rval,records,locator.\n"
      << "  placement-audit --audit-pg-num N: offline. For every base\n"
      << "    vector, compare the PG its object sits in once the pool has N\n"
      << "    PGs with the PG a fresh --pg-num N index would route it to.\n";
}

template <typename T>
bool parse_unsigned(std::string_view text, T *value)
{
  static_assert(std::is_unsigned_v<T>);
  if (value == nullptr || text.empty()) {
    return false;
  }
  T parsed = 0;
  const auto result = std::from_chars(
      text.data(), text.data() + text.size(), parsed);
  if (result.ec != std::errc() || result.ptr != text.data() + text.size()) {
    return false;
  }
  *value = parsed;
  return true;
}

bool parse_options(int argc, char **argv, options_t *options)
{
  enum : int {
    OPT_BASE = 1000, OPT_QUERY, OPT_GROUNDTRUTH, OPT_POOL, OPT_BUCKET,
    OPT_INDEX, OPT_KEY_PREFIX, OPT_CLIENT, OPT_CLUSTER, OPT_CONF,
    OPT_OPERATION, OPT_ROUTING_MODE, OPT_QUANTIZE, OPT_RERANK_TOP_N,
    OPT_PG_NUM, OPT_K, OPT_L,
    OPT_SEED,
    OPT_D, OPT_M, OPT_HAMMING_RADIUS, OPT_DISTANCE_BUCKET_BITS,
    OPT_RESIDUAL_BITS, OPT_DISTANCE_BUCKET_RADIUS,
    OPT_RESIDUAL_HAMMING_RADIUS, OPT_PROBE_LIMIT_PER_PG,
    OPT_PIVOT_BUILD_SAMPLE, OPT_PIVOT_PROBE_BUDGET,
    OPT_PIVOT_REFINE_ITERS, OPT_PIVOT_SPHERICAL, OPT_SIM_M_LIST,
    OPT_PIVOT_BALANCE_EPS, OPT_SIM_PLACE_CAP_EPS, OPT_SIM_RADIUS_ALPHA,
    OPT_SIM_RADIUS_Q, OPT_SIM_MULTI_REP, OPT_TOP_K,
    OPT_QUERY_CONCURRENCY, OPT_LOAD_CONCURRENCY, OPT_WRITE_RANK_BEGIN,
    OPT_WARMUP_ROUNDS, OPT_ROUNDS, OPT_MIN_QUERIES,
    OPT_MIN_SECONDS, OPT_BASE_LIMIT, OPT_QUERY_LIMIT,
    OPT_QUERY_INDEX_LIST, OPT_DESTINATION_PG_LOG,
    OPT_FANOUT_MODE, OPT_IO_DEPTH, OPT_QUERY_BATCH_SIZE, OPT_QUERY_RESULTS_CSV,
    OPT_FANOUT_LOG, OPT_FANOUT_LOG_COUNT,
    OPT_QUERY_TABLE_COUNT, OPT_MARGIN_ORDERED_PROBES,
    OPT_SELECTIVE, OPT_SELECTIVE_SCHEDULE, OPT_SELECTIVE_STOP,
    OPT_SELECTIVE_MIN_IMPROVEMENT, OPT_SELECTIVE_CSV,
    OPT_SERVER_SIDE_BOUNDARY_SEARCH, OPT_TREE_SEARCH_BUDGET,
    OPT_RAW_EUCLIDEAN_GEOMETRY, OPT_TREE_PRUNE,
    OPT_TREE_NO_IMPROVE_PATIENCE, OPT_TREE_ONODE_BUDGET,
    OPT_PARTITION_NUM, OPT_OBJECT_LIST, OPT_AUDIT_PG_NUM,
    OPT_BUCKET_LOCATORS, OPT_LSH_ALIGNED_LOCATORS, OPT_FULL_PG_SCAN,
    OPT_REPLY_INCLUDE_VECTOR, OPT_REPLY_OMIT_TREE_STATS, OPT_MEASURE_HOOK,
    OPT_WARMUP_QUERIES, OPT_HELP,
    OPT_PLACEMENT_MODE, OPT_HIERARCHICAL_SCALE,
  };
  const option long_options[] = {
    {"base", required_argument, nullptr, OPT_BASE},
    {"query", required_argument, nullptr, OPT_QUERY},
    {"groundtruth", required_argument, nullptr, OPT_GROUNDTRUTH},
    {"pool", required_argument, nullptr, OPT_POOL},
    {"bucket", required_argument, nullptr, OPT_BUCKET},
    {"index", required_argument, nullptr, OPT_INDEX},
    {"key-prefix", required_argument, nullptr, OPT_KEY_PREFIX},
    {"client", required_argument, nullptr, OPT_CLIENT},
    {"cluster", required_argument, nullptr, OPT_CLUSTER},
    {"conf", required_argument, nullptr, OPT_CONF},
    {"operation", required_argument, nullptr, OPT_OPERATION},
    {"routing-mode", required_argument, nullptr, OPT_ROUTING_MODE},
    {"quantize", required_argument, nullptr, OPT_QUANTIZE},
    {"rerank-top-n", required_argument, nullptr, OPT_RERANK_TOP_N},
    {"pg-num", required_argument, nullptr, OPT_PG_NUM},
    {"partition-num", required_argument, nullptr, OPT_PARTITION_NUM},
    {"bucket-locators", no_argument, nullptr, OPT_BUCKET_LOCATORS},
    {"lsh-aligned-locators", no_argument, nullptr, OPT_LSH_ALIGNED_LOCATORS},
    {"placement-mode", required_argument, nullptr, OPT_PLACEMENT_MODE},
    {"hierarchical-scale", required_argument, nullptr,
     OPT_HIERARCHICAL_SCALE},
    {"full-pg-scan", no_argument, nullptr, OPT_FULL_PG_SCAN},
    {"object-list", required_argument, nullptr, OPT_OBJECT_LIST},
    {"audit-pg-num", required_argument, nullptr, OPT_AUDIT_PG_NUM},
    {"k", required_argument, nullptr, OPT_K},
    {"l", required_argument, nullptr, OPT_L},
    {"seed", required_argument, nullptr, OPT_SEED},
    {"d", required_argument, nullptr, OPT_D},
    {"m", required_argument, nullptr, OPT_M},
    {"hamming-radius", required_argument, nullptr, OPT_HAMMING_RADIUS},
    {"distance-bucket-bits", required_argument, nullptr, OPT_DISTANCE_BUCKET_BITS},
    {"residual-bits", required_argument, nullptr, OPT_RESIDUAL_BITS},
    {"distance-bucket-radius", required_argument, nullptr, OPT_DISTANCE_BUCKET_RADIUS},
    {"residual-hamming-radius", required_argument, nullptr, OPT_RESIDUAL_HAMMING_RADIUS},
    {"probe-limit-per-pg", required_argument, nullptr, OPT_PROBE_LIMIT_PER_PG},
    {"pivot-build-sample", required_argument, nullptr, OPT_PIVOT_BUILD_SAMPLE},
    {"pivot-probe-budget", required_argument, nullptr, OPT_PIVOT_PROBE_BUDGET},
    {"pivot-refine-iters", required_argument, nullptr, OPT_PIVOT_REFINE_ITERS},
    {"pivot-spherical", no_argument, nullptr, OPT_PIVOT_SPHERICAL},
    {"sim-m-list", required_argument, nullptr, OPT_SIM_M_LIST},
    {"pivot-balance-eps", required_argument, nullptr, OPT_PIVOT_BALANCE_EPS},
    {"sim-place-cap-eps", required_argument, nullptr, OPT_SIM_PLACE_CAP_EPS},
    {"pivot-place-cap-eps", required_argument, nullptr, OPT_SIM_PLACE_CAP_EPS},
    {"sim-radius-alpha", required_argument, nullptr, OPT_SIM_RADIUS_ALPHA},
    {"sim-radius-q", required_argument, nullptr, OPT_SIM_RADIUS_Q},
    {"sim-multi-rep", required_argument, nullptr, OPT_SIM_MULTI_REP},
    {"top-k", required_argument, nullptr, OPT_TOP_K},
    {"query-concurrency", required_argument, nullptr, OPT_QUERY_CONCURRENCY},
    {"load-concurrency", required_argument, nullptr, OPT_LOAD_CONCURRENCY},
    {"write-rank-begin", required_argument, nullptr, OPT_WRITE_RANK_BEGIN},
    {"warmup-rounds", required_argument, nullptr, OPT_WARMUP_ROUNDS},
    {"rounds", required_argument, nullptr, OPT_ROUNDS},
    {"min-queries", required_argument, nullptr, OPT_MIN_QUERIES},
    {"min-seconds", required_argument, nullptr, OPT_MIN_SECONDS},
    {"base-limit", required_argument, nullptr, OPT_BASE_LIMIT},
    {"query-limit", required_argument, nullptr, OPT_QUERY_LIMIT},
    {"query-index-list", required_argument, nullptr, OPT_QUERY_INDEX_LIST},
    {"destination-pg-log", required_argument, nullptr, OPT_DESTINATION_PG_LOG},
    {"fanout-mode", required_argument, nullptr, OPT_FANOUT_MODE},
    {"io-depth", required_argument, nullptr, OPT_IO_DEPTH},
    {"query-batch-size", required_argument, nullptr, OPT_QUERY_BATCH_SIZE},
    {"query-results-csv", required_argument, nullptr, OPT_QUERY_RESULTS_CSV},
    {"fanout-log", required_argument, nullptr, OPT_FANOUT_LOG},
    {"fanout-log-count", required_argument, nullptr, OPT_FANOUT_LOG_COUNT},
    {"query-table-count", required_argument, nullptr, OPT_QUERY_TABLE_COUNT},
    {"margin-ordered-probes", no_argument, nullptr, OPT_MARGIN_ORDERED_PROBES},
    {"selective", no_argument, nullptr, OPT_SELECTIVE},
    {"selective-schedule", required_argument, nullptr, OPT_SELECTIVE_SCHEDULE},
    {"selective-stop", required_argument, nullptr, OPT_SELECTIVE_STOP},
    {"selective-min-improvement", required_argument, nullptr,
     OPT_SELECTIVE_MIN_IMPROVEMENT},
    {"selective-csv", required_argument, nullptr, OPT_SELECTIVE_CSV},
    {"server-side-boundary-search", no_argument, nullptr,
     OPT_SERVER_SIDE_BOUNDARY_SEARCH},
    {"tree-search-budget", required_argument, nullptr,
     OPT_TREE_SEARCH_BUDGET},
    {"raw-euclidean-geometry", no_argument, nullptr,
     OPT_RAW_EUCLIDEAN_GEOMETRY},
    {"tree-prune", no_argument, nullptr, OPT_TREE_PRUNE},
    {"reply-include-vector", no_argument, nullptr, OPT_REPLY_INCLUDE_VECTOR},
    {"reply-omit-tree-stats", no_argument, nullptr,
     OPT_REPLY_OMIT_TREE_STATS},
    {"measure-hook", required_argument, nullptr, OPT_MEASURE_HOOK},
    {"warmup-queries", required_argument, nullptr, OPT_WARMUP_QUERIES},
    {"tree-no-improve-patience", required_argument, nullptr,
     OPT_TREE_NO_IMPROVE_PATIENCE},
    {"tree-onode-budget", required_argument, nullptr, OPT_TREE_ONODE_BUDGET},
    {"help", no_argument, nullptr, OPT_HELP},
    {nullptr, 0, nullptr, 0},
  };

  for (;;) {
    const int c = getopt_long(argc, argv, "", long_options, nullptr);
    if (c == -1) break;
    switch (c) {
    case OPT_BASE: options->base_path = optarg; break;
    case OPT_QUERY: options->query_path = optarg; break;
    case OPT_GROUNDTRUTH: options->groundtruth_path = optarg; break;
    case OPT_POOL: options->pool = optarg; break;
    case OPT_BUCKET: options->bucket = optarg; break;
    case OPT_INDEX: options->index = optarg; break;
    case OPT_KEY_PREFIX: options->key_prefix = optarg; break;
    case OPT_CLIENT: options->client_name = optarg; break;
    case OPT_CLUSTER: options->cluster_name = optarg; break;
    case OPT_CONF: options->conf_path = optarg; break;
    case OPT_OPERATION: options->operation = optarg; break;
    case OPT_ROUTING_MODE: options->routing_mode = optarg; break;
    case OPT_QUANTIZE: options->quantize = optarg; break;
#define PARSE_OPT(field) \
    if (!parse_unsigned(std::string_view(optarg), &options->field)) return false
    case OPT_RERANK_TOP_N: PARSE_OPT(rerank_top_n); break;
    case OPT_PG_NUM: PARSE_OPT(pg_num); break;
    case OPT_PARTITION_NUM: PARSE_OPT(partition_num); break;
    case OPT_AUDIT_PG_NUM: PARSE_OPT(audit_pg_num); break;
    case OPT_K: PARSE_OPT(k); break;
    case OPT_L: PARSE_OPT(l); break;
    case OPT_SEED: PARSE_OPT(seed); break;
    case OPT_D: PARSE_OPT(d); break;
    case OPT_M: PARSE_OPT(m); break;
    case OPT_WARMUP_QUERIES: PARSE_OPT(warmup_queries); break;
    case OPT_HAMMING_RADIUS: PARSE_OPT(hamming_radius); break;
    case OPT_DISTANCE_BUCKET_BITS: PARSE_OPT(distance_bucket_bits); break;
    case OPT_RESIDUAL_BITS: PARSE_OPT(residual_bits); break;
    case OPT_DISTANCE_BUCKET_RADIUS: PARSE_OPT(distance_bucket_radius); break;
    case OPT_RESIDUAL_HAMMING_RADIUS: PARSE_OPT(residual_hamming_radius); break;
    case OPT_PROBE_LIMIT_PER_PG: PARSE_OPT(probe_limit_per_pg); break;
    case OPT_PIVOT_BUILD_SAMPLE: PARSE_OPT(pivot_build_sample); break;
    case OPT_PIVOT_PROBE_BUDGET: PARSE_OPT(pivot_probe_budget); break;
    case OPT_PIVOT_REFINE_ITERS: PARSE_OPT(pivot_refine_iters); break;
    case OPT_PIVOT_SPHERICAL: options->pivot_spherical = true; break;
    case OPT_SIM_M_LIST: options->sim_m_list = optarg; break;
    case OPT_PIVOT_BALANCE_EPS: options->pivot_balance_eps = std::atof(optarg); break;
    case OPT_SIM_PLACE_CAP_EPS: options->sim_place_cap_eps = std::atof(optarg); break;
    case OPT_SIM_RADIUS_ALPHA: options->sim_radius_alpha = std::atof(optarg); break;
    case OPT_SIM_RADIUS_Q: options->sim_radius_q = std::atof(optarg); break;
    case OPT_SIM_MULTI_REP: PARSE_OPT(sim_multi_rep); break;
    case OPT_TOP_K: PARSE_OPT(top_k); break;
    case OPT_QUERY_CONCURRENCY: PARSE_OPT(query_concurrency); break;
    case OPT_LOAD_CONCURRENCY: PARSE_OPT(load_concurrency); break;
    case OPT_WRITE_RANK_BEGIN: PARSE_OPT(write_rank_begin); break;
    case OPT_WARMUP_ROUNDS: PARSE_OPT(warmup_rounds); break;
    case OPT_ROUNDS: PARSE_OPT(rounds); break;
    case OPT_QUERY_TABLE_COUNT: PARSE_OPT(query_table_count); break;
    case OPT_MIN_QUERIES: PARSE_OPT(min_queries); break;
    case OPT_MIN_SECONDS: PARSE_OPT(min_seconds); break;
    case OPT_BASE_LIMIT: PARSE_OPT(base_limit); break;
    case OPT_QUERY_LIMIT: PARSE_OPT(query_limit); break;
    case OPT_TREE_SEARCH_BUDGET: PARSE_OPT(tree_search_budget); break;
    case OPT_IO_DEPTH: PARSE_OPT(io_depth); break;
    case OPT_QUERY_BATCH_SIZE: PARSE_OPT(query_batch_size); break;
    case OPT_QUERY_RESULTS_CSV:
      options->query_results_csv_path = optarg; break;
    case OPT_TREE_NO_IMPROVE_PATIENCE:
      PARSE_OPT(tree_no_improve_patience); break;
    case OPT_TREE_ONODE_BUDGET: PARSE_OPT(tree_onode_budget); break;
#undef PARSE_OPT
    case OPT_QUERY_INDEX_LIST: options->query_index_list_path = optarg; break;
    case OPT_OBJECT_LIST: options->object_list_path = optarg; break;
    case OPT_BUCKET_LOCATORS:
      options->bucket_locators = pg_lsh::bucket_locators_hashed;
      break;
    case OPT_LSH_ALIGNED_LOCATORS:
      options->bucket_locators = pg_lsh::bucket_locators_lsh_aligned;
      break;
    case OPT_FULL_PG_SCAN: options->full_pg_scan = true; break;
    case OPT_PLACEMENT_MODE:
      if (std::string_view(optarg) == "fixed_lsh") {
        options->placement_mode = pg_lsh::placement_mode_fixed_lsh;
      } else if (std::string_view(optarg) == "hierarchical_lsh") {
        options->placement_mode = pg_lsh::placement_mode_hierarchical_lsh;
      } else if (std::string_view(optarg) == "training_derived") {
        options->placement_mode = pg_lsh::placement_mode_training_derived;
      } else {
        return false;
      }
      break;
    case OPT_HIERARCHICAL_SCALE: {
      char *end = nullptr;
      options->hierarchical_scale = std::strtod(optarg, &end);
      if (end == optarg || *end != '\0' ||
          !(options->hierarchical_scale > 0) ||
          !std::isfinite(options->hierarchical_scale)) {
        return false;
      }
      break;
    }
    case OPT_DESTINATION_PG_LOG: options->destination_pg_log_path = optarg; break;
    case OPT_FANOUT_MODE: options->fanout_mode = optarg; break;
    case OPT_FANOUT_LOG: options->fanout_log_path = optarg; break;
    case OPT_FANOUT_LOG_COUNT:
      if (!parse_unsigned(std::string_view(optarg), &options->fanout_log_count)) {
        return false;
      }
      break;
    case OPT_MARGIN_ORDERED_PROBES:
      options->margin_ordered_probes = true;
      break;
    case OPT_SELECTIVE: options->selective = true; break;
    case OPT_SELECTIVE_SCHEDULE: options->selective_schedule = optarg; break;
    case OPT_SELECTIVE_STOP: options->selective_stop = optarg; break;
    case OPT_SELECTIVE_MIN_IMPROVEMENT:
      options->selective_min_improvement = std::atof(optarg);
      break;
    case OPT_SELECTIVE_CSV: options->selective_csv_path = optarg; break;
    case OPT_SERVER_SIDE_BOUNDARY_SEARCH:
      options->server_side_boundary_search = true;
      break;
    case OPT_RAW_EUCLIDEAN_GEOMETRY:
      options->raw_euclidean_geometry = true;
      break;
    case OPT_REPLY_INCLUDE_VECTOR: options->reply_include_vector = true; break;
    case OPT_REPLY_OMIT_TREE_STATS: options->reply_omit_tree_stats = true; break;
    case OPT_MEASURE_HOOK: options->measure_hook = optarg; break;
    case OPT_TREE_PRUNE:
      options->tree_prune = true;
      break;
    case OPT_HELP: usage(std::cout, argv[0]); std::exit(0);
    default: return false;
    }
  }

  if (optind != argc || options->base_path.empty() ||
      (options->pool.empty() && options->operation != "route-sim")) {
    return false;
  }
  if (options->operation == "route-sim" && options->routing_mode != "pivot") {
    std::cerr << "--operation route-sim needs --routing-mode pivot\n";
    return false;
  }
  if (options->operation != "load" && options->operation != "query" &&
      options->operation != "both" && options->operation != "fanout-query" &&
      options->operation != "object-census" &&
      options->operation != "placement-audit" &&
      options->operation != "route-sim") {
    return false;
  }
  if (options->operation == "object-census" &&
      options->object_list_path.empty()) {
    return false;
  }
  if (options->full_pg_scan &&
      (!options->bucket_locators || options->object_list_path.empty() ||
       options->tree_search_budget != 0)) {
    return false;
  }
  if (options->operation == "placement-audit" &&
      (options->audit_pg_num == 0 ||
       (options->audit_pg_num & (options->audit_pg_num - 1)) != 0 ||
       options->d != 1)) {
    return false;
  }
  if (options->bucket_locators &&
      (options->partition_num != 0 ||
       options->routing_mode != "pg-suboid")) {
    return false;
  }
  if (options->partition_num != 0 &&
      ((options->partition_num & (options->partition_num - 1)) != 0 ||
       options->partition_num < options->pg_num ||
       options->routing_mode == "pivot")) {
    return false;
  }
  if (options->operation == "fanout-query" &&
      options->fanout_mode != "sequential" && options->fanout_mode != "parallel") {
    return false;
  }
  if (options->quantize != "float32" && options->quantize != "int8") {
    std::cerr << "--quantize must be float32 or int8\n";
    return false;
  }
  if (options->rerank_top_n > 0) {
    // Only the two paths that were wired for it ask the OSD for a deeper
    // shortlist; anywhere else the option would silently do nothing.
    if (options->operation != "fanout-query") {
      std::cerr << "--rerank-top-n only applies to --operation fanout-query\n";
      return false;
    }
    if (options->rerank_top_n < options->top_k) {
      std::cerr << "--rerank-top-n must be >= --top-k\n";
      return false;
    }
  }
  if (options->routing_mode != "pg-container" &&
      options->routing_mode != "pg-suboid" &&
      options->routing_mode != "pivot") {
    return false;
  }
  if (options->operation != "load" && options->operation != "object-census" &&
      options->operation != "placement-audit" && options->query_path.empty()) {
    return false;
  }
  // m>1 fans one logical query out to m distinct candidate PGs
  // (multi-probe LSH); every other operation keeps today's single-probe
  // (m=1) contract unchanged.
  if (options->pg_num == 0 ||
      (options->pg_num & (options->pg_num - 1)) != 0 ||
      options->d == 0 || options->d > options->pg_num ||
      (options->operation != "fanout-query" && options->m != 1) ||
      (options->operation == "fanout-query" &&
       (options->m == 0 || options->m > options->pg_num)) ||
      options->hamming_radius > options->k) {
    return false;
  }
  if (options->routing_mode == "pg-container" ||
      options->routing_mode == "pivot") {
    // Both are one-object-per-PG (no sub-OID filtering): pivot routing
    // only changes which PG a vector lands in, not the in-PG storage
    // layout.
    if (options->residual_bits != 0 || options->probe_limit_per_pg != 0 ||
        options->distance_bucket_bits != 0 ||
        options->distance_bucket_radius != 0 ||
        options->residual_hamming_radius != 0) {
      return false;
    }
  } else if (options->residual_bits != 4 && options->residual_bits != 0) {
    // residual_bits==0 is legal: the residual wildcard-mask and Hamming
    // radius logic is skipped entirely, which isolates distance_bucket
    // effects in a microbenchmark. 4 stays the default.
    return false;
  }
  if (options->routing_mode == "pivot" &&
      options->pivot_probe_budget == 0) {
    return false;
  }
  // sequential issues one probe at a time through the blocking
  // query_sync() path, so a queue depth means nothing there.
  if (options->io_depth > 0 &&
      (options->operation != "fanout-query" ||
       options->fanout_mode != "parallel")) {
    std::cerr << "error: --io-depth needs --operation fanout-query"
                 " --fanout-mode parallel\n";
    return false;
  }
  // Batching only makes sense where several probes can share one request
  // and the dispatcher is free to resolve ahead: --selective makes a
  // query's stages strictly sequential, and --io-depth owns the same
  // in-flight window this would.
  if (options->query_batch_size == 0) {
    std::cerr << "error: --query-batch-size must be >= 1\n";
    return false;
  }
  if (options->query_batch_size > 1) {
    if (options->operation != "fanout-query" ||
        options->fanout_mode != "parallel") {
      std::cerr << "error: --query-batch-size needs --operation fanout-query"
                   " --fanout-mode parallel\n";
      return false;
    }
    if (options->selective) {
      std::cerr << "error: --query-batch-size is incompatible with"
                   " --selective\n";
      return false;
    }
    if (options->io_depth != 0) {
      std::cerr << "error: --query-batch-size is incompatible with"
                   " --io-depth\n";
      return false;
    }
    // Its per-subrequest submit/complete timeline is a property of the
    // one-probe-per-request dispatcher; rather than emit a file that
    // silently means something else, refuse it.
    if (!options->fanout_log_path.empty()) {
      std::cerr << "error: --query-batch-size is incompatible with"
                   " --fanout-log\n";
      return false;
    }
  }
  // [begin, d) has to be a non-empty range, and any other operation would
  // silently ignore the option.
  if (options->write_rank_begin > 0) {
    if (options->write_rank_begin >= options->d) {
      std::cerr << "error: --write-rank-begin must be < --d\n";
      return false;
    }
    if (options->operation != "load") {
      std::cerr << "error: --write-rank-begin is only valid with"
                   " --operation load\n";
      return false;
    }
  }
  // Boundary-aware search only exists for pg-suboid's sub_oid key space
  // (see common/vector_pg_lsh_boundary.h).
  if (options->server_side_boundary_search &&
      options->routing_mode != "pg-suboid") {
    return false;
  }
  // raw_euclidean_geometry needs a sub_oid distance_bucket axis to place
  // anything into (see librados/vector_pg_lsh.h's
  // pg_lsh_layout_version_raw_euclidean_v1), same precondition as
  // server_side_boundary_search above.
  if (options->raw_euclidean_geometry &&
      (options->routing_mode != "pg-suboid" ||
       options->distance_bucket_bits == 0)) {
    return false;
  }
  // Real tau-based exclusion is only provably safe under raw_euclidean_v1
  // -- see librados/vector_pg_lsh.h's query_params_t::
  // tree_boundary_search_prune / validate_query_params().
  if (!options->measure_hook.empty() &&
      (options->operation != "fanout-query" ||
       options->query_concurrency != 1 || options->query_batch_size > 1)) {
    std::cerr << "error: --measure-hook requires fanout-query, "
              << "--query-concurrency 1 and no --query-batch-size\n";
    return false;
  }
  if (options->tree_prune &&
      (!options->raw_euclidean_geometry ||
       options->tree_search_budget == 0)) {
    return false;
  }
  if (options->query_table_count > options->l) {
    return false;
  }
  // S is persisted, never derived: hierarchical_lsh needs it explicitly,
  // and fixed_lsh rejects it. Only pg-suboid goes through index_config_t.
  if ((options->placement_mode ==
         pg_lsh::placement_mode_hierarchical_lsh) !=
      (options->hierarchical_scale > 0)) {
    return false;
  }
  if (options->placement_mode == pg_lsh::placement_mode_hierarchical_lsh &&
      options->routing_mode != "pg-suboid") {
    return false;
  }
  if (options->placement_mode == pg_lsh::placement_mode_training_derived &&
      (options->routing_mode != "pg-suboid" ||
       options->pivot_build_sample == 0)) {
    return false;
  }
  if (options->selective) {
    if (options->operation != "fanout-query") {
      return false;
    }
    if (options->selective_stop != "never" &&
        options->selective_stop != "tau-relative") {
      return false;
    }
    ceph::rados::vector_selective_fanout::schedule_t schedule;
    if (ceph::rados::vector_selective_fanout::parse_schedule(
          options->selective_schedule, &schedule) < 0) {
      return false;
    }
    // The last stage must be exactly --m. The schedule slices one
    // build_query_probes(m) result, so a tail past m would name PGs that
    // were never routed, and a tail short of m would silently make --m
    // (which labels every number this run reports) a lie.
    if (schedule.back() != options->m) {
      return false;
    }
    if (!(options->selective_min_improvement >= 0.0) ||
        !std::isfinite(options->selective_min_improvement)) {
      return false;
    }
  }
  if (options->routing_mode == "pivot" &&
      options->pivot_build_sample == 0) {
    return false;
  }
  return options->top_k > 0 && options->k > 0 && options->l > 0 &&
    options->query_concurrency > 0 && options->load_concurrency > 0 &&
    options->rounds > 0;
}

uint32_t decode_le32(const unsigned char bytes[4])
{
  return static_cast<uint32_t>(bytes[0]) |
    (static_cast<uint32_t>(bytes[1]) << 8) |
    (static_cast<uint32_t>(bytes[2]) << 16) |
    (static_cast<uint32_t>(bytes[3]) << 24);
}

bool read_exact(std::ifstream& input, void *buffer, size_t length)
{
  input.read(static_cast<char *>(buffer), length);
  return input.gcount() == static_cast<std::streamsize>(length);
}

std::optional<float_dataset_t> read_fvecs(
    const std::string& path, size_t limit, std::string *error)
{
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    *error = "cannot open " + path;
    return std::nullopt;
  }
  float_dataset_t data;
  while (limit == 0 || data.rows.size() < limit) {
    unsigned char encoded[4];
    input.read(reinterpret_cast<char *>(encoded), sizeof(encoded));
    if (input.gcount() == 0 && input.eof()) break;
    if (input.gcount() != static_cast<std::streamsize>(sizeof(encoded))) {
      *error = "truncated dimension in " + path;
      return std::nullopt;
    }
    const uint32_t dimension = decode_le32(encoded);
    if (dimension == 0 || dimension > LIBRADOS_VECTOR_MAX_DIMENSION ||
        (data.dimension != 0 && data.dimension != dimension)) {
      *error = "invalid or inconsistent dimension in " + path;
      return std::nullopt;
    }
    data.dimension = dimension;
    std::vector<float> row(dimension);
    if (!read_exact(input, row.data(), row.size() * sizeof(float))) {
      *error = "truncated vector in " + path;
      return std::nullopt;
    }
    data.rows.push_back(std::move(row));
  }
  if (data.rows.empty()) {
    *error = "empty dataset " + path;
    return std::nullopt;
  }
  return data;
}

std::optional<ivec_dataset_t> read_ivecs(
    const std::string& path, size_t limit, std::string *error)
{
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    *error = "cannot open " + path;
    return std::nullopt;
  }
  ivec_dataset_t data;
  while (limit == 0 || data.rows.size() < limit) {
    unsigned char encoded[4];
    input.read(reinterpret_cast<char *>(encoded), sizeof(encoded));
    if (input.gcount() == 0 && input.eof()) break;
    if (input.gcount() != static_cast<std::streamsize>(sizeof(encoded))) {
      *error = "truncated ivecs dimension in " + path;
      return std::nullopt;
    }
    const uint32_t count = decode_le32(encoded);
    if (count == 0 || count > (1U << 20)) {
      *error = "invalid ivecs dimension in " + path;
      return std::nullopt;
    }
    std::vector<uint32_t> row(count);
    for (uint32_t& value : row) {
      if (!read_exact(input, encoded, sizeof(encoded))) {
        *error = "truncated ivecs row in " + path;
        return std::nullopt;
      }
      value = decode_le32(encoded);
    }
    data.rows.push_back(std::move(row));
  }
  if (data.rows.empty()) {
    *error = "empty ground truth " + path;
    return std::nullopt;
  }
  return data;
}

// Newline-separated 0-based row indices into a query fvecs file. Used to
// build a curated workload (e.g. "every one of these rows routes to PG X")
// instead of run_queries()'s default "first --query-limit rows, cycled".
std::optional<std::vector<size_t>> read_index_list(
    const std::string& path, std::string *error)
{
  std::ifstream input(path);
  if (!input) {
    *error = "cannot open " + path;
    return std::nullopt;
  }
  std::vector<size_t> indices;
  size_t value = 0;
  while (input >> value) indices.push_back(value);
  if (!input.eof()) {
    *error = "malformed index in " + path;
    return std::nullopt;
  }
  if (indices.empty()) {
    *error = "empty index list " + path;
    return std::nullopt;
  }
  return indices;
}

// Selects specific rows (by index, in the given order) out of an
// already-loaded dataset, for --query-index-list.
std::optional<float_dataset_t> select_rows(
    const float_dataset_t& source,
    const std::vector<size_t>& indices,
    std::string *error)
{
  float_dataset_t out;
  out.dimension = source.dimension;
  out.rows.reserve(indices.size());
  for (const size_t index : indices) {
    if (index >= source.rows.size()) {
      *error = "index out of range: " + std::to_string(index);
      return std::nullopt;
    }
    out.rows.push_back(source.rows[index]);
  }
  return out;
}

std::string error_text(int r)
{
  return r < 0 ? std::string(std::strerror(-r)) : "success";
}

std::string vector_key(const options_t& options, size_t index)
{
  std::ostringstream out;
  out << options.key_prefix << std::setw(8) << std::setfill('0') << index;
  return out.str();
}

// entry_id is a deterministic function of (bucket_name, index_name, key)
// computed the same way at PUT time (common/vector_record.h's
// vector_entry_id()) and returned verbatim by every query. Recall compares
// returned entry_ids against the entry_id expected for each groundtruth
// row, rather than parsing entry.key, whose server-side restoration from
// OMAP is unreliable.
std::string expected_entry_id(const options_t& options, uint32_t row)
{
  ceph::rados::put_vector_request_t req;
  req.bucket_name = options.bucket;
  req.index_name = options.index;
  req.key = vector_key(options, row);
  return ceph::os::vector_entry_id(req);
}

ceph::bufferlist vector_buffer(const std::vector<float>& vector)
{
  ceph::bufferlist out;
  out.append(reinterpret_cast<const char *>(vector.data()),
             vector.size() * sizeof(float));
  return out;
}

// SQ8 uses ONE global symmetric scale for every stored vector and every
// query, so the OSD never needs to know it: a uniform change of units
// leaves the euclidean ranking untouched. The scale is the 99.9th
// percentile of |v| over the first rows of --base, so a load run and a
// query run against the same file derive the same number without
// persisting it anywhere.
float quantization_scale(const float_dataset_t& base)
{
  constexpr size_t kSampleRows = 20000;
  const size_t rows = std::min<size_t>(kSampleRows, base.rows.size());
  std::vector<float> magnitudes;
  magnitudes.reserve(rows * base.dimension);
  for (size_t row = 0; row < rows; ++row) {
    for (const float value : base.rows[row]) {
      magnitudes.push_back(std::fabs(value));
    }
  }
  if (magnitudes.empty()) {
    return 1.0f;
  }
  const size_t index = static_cast<size_t>(
      0.999 * static_cast<double>(magnitudes.size() - 1));
  std::nth_element(magnitudes.begin(), magnitudes.begin() + index,
                   magnitudes.end());
  const float scale = magnitudes[index];
  return scale > 0 ? scale : 1.0f;
}

// ---- client-side float32 rerank of an SQ8 shortlist ---------------------
//
// SQ8 loses recall in two distinguishable ways. Routing decides which
// vectors are even scanned, and quantization decides how the scanned ones
// are ORDERED. A rerank fixes only the second: ask the OSD for a deeper
// shortlist than top_k, then rescore that shortlist against the original
// float32 vectors. Its ceiling is therefore the float32 recall at the same
// M -- it can recover the ordering loss and nothing else.
//
// entry_id is a deterministic hash of (bucket, index, key) and key is a
// deterministic function of the base row (see expected_entry_id()), so the
// row a returned entry came from is recoverable without an OMAP join --
// which matters because these runs have the identity join commented out.
struct rerank_context_t {
  const float_dataset_t *base = nullptr;
  std::unordered_map<std::string, uint32_t> row_of_entry_id;
};

std::shared_ptr<const rerank_context_t> build_rerank_context(
    const options_t& options, const float_dataset_t& base)
{
  auto context = std::make_shared<rerank_context_t>();
  context->base = &base;
  context->row_of_entry_id.reserve(base.rows.size() * 2);
  for (size_t row = 0; row < base.rows.size(); ++row) {
    context->row_of_entry_id.emplace(
        expected_entry_id(options, static_cast<uint32_t>(row)),
        static_cast<uint32_t>(row));
  }
  return context;
}

// Rescores `merged` against the unquantized query and returns the best
// `top_k` entry_ids, best first. An entry whose row cannot be recovered
// keeps its server-side rank behind every rescored one, so a lookup miss
// degrades to "not reranked" instead of silently dropping a result;
// `unmapped` counts them so the run can report it.
template <typename Entries>
std::vector<const std::string *> rerank_shortlist(
    const rerank_context_t& context,
    const std::vector<float>& query_row,
    const Entries& merged,
    uint32_t top_k,
    uint64_t *unmapped)
{
  // Squared L2 is enough: it is monotone in L2, and the shortlist is
  // rescored only to be ordered.
  std::vector<std::pair<double, const std::string *>> scored;
  std::vector<const std::string *> unscored;
  scored.reserve(merged.size());
  for (const auto& entry : merged) {
    const auto it = context.row_of_entry_id.find(entry.entry_id);
    if (it == context.row_of_entry_id.end()) {
      unscored.push_back(&entry.entry_id);
      if (unmapped != nullptr) {
        ++*unmapped;
      }
      continue;
    }
    const std::vector<float>& stored = context.base->rows[it->second];
    const size_t dimension = std::min(stored.size(), query_row.size());
    double sum_sq = 0;
    for (size_t i = 0; i < dimension; ++i) {
      const double diff = double(query_row[i]) - double(stored[i]);
      sum_sq += diff * diff;
    }
    scored.emplace_back(sum_sq, &entry.entry_id);
  }
  std::sort(scored.begin(), scored.end(),
            [](const auto& lhs, const auto& rhs) {
              if (lhs.first != rhs.first) {
                return lhs.first < rhs.first;
              }
              return *lhs.second < *rhs.second;
            });

  std::vector<const std::string *> best;
  best.reserve(std::min<size_t>(top_k, scored.size() + unscored.size()));
  for (const auto& entry : scored) {
    if (best.size() >= top_k) {
      break;
    }
    best.push_back(entry.second);
  }
  for (const auto *entry_id : unscored) {
    if (best.size() >= top_k) {
      break;
    }
    best.push_back(entry_id);
  }
  return best;
}

// How deep a shortlist the OSD is asked for. Without rerank this is
// top_k, exactly as before.
uint32_t shortlist_top_k(const options_t& options)
{
  return options.rerank_top_n > 0 ? options.rerank_top_n : options.top_k;
}

// pg-suboid placement (LSH tables, sub_oid) is defined on float32 vectors
// and rejects int8 bytes. Under --quantize int8 routing therefore uses the
// unquantized vector against a float32 view of the index config; only the
// stored/queried payload is int8. Pivot routing already works on floats.
pg_lsh::index_config_t placement_config(const pg_lsh::index_config_t& config)
{
  auto c = config;
  c.data_type = ceph::rados::vector_data_type_float32;
  return c;
}

ceph::bufferlist vector_buffer_typed(const std::vector<float>& vector,
                                     uint32_t data_type,
                                     float scale)
{
  if (data_type != ceph::rados::vector_data_type_int8) {
    return vector_buffer(vector);
  }
  const float factor = 127.0f / scale;
  std::vector<int8_t> codes(vector.size());
  for (size_t i = 0; i < vector.size(); ++i) {
    const long code = std::lround(vector[i] * factor);
    codes[i] = static_cast<int8_t>(std::clamp<long>(code, -127, 127));
  }
  ceph::bufferlist out;
  out.append(reinterpret_cast<const char *>(codes.data()), codes.size());
  return out;
}

pg_lsh::pool_pg_info_t pool_info(const options_t& options)
{
  return {options.pg_num, options.pg_num, 0};
}

// Coordinate-wise mean (anchor) and a max-squared-distance scale for
// pg_lsh_layout_version_raw_euclidean_v1 placement, from a strided --base
// sample. A centroid anchor keeps distance_bucket spread: with a
// near-origin anchor, ||v-anchor||^2 is dominated by ||v||^2 alone on
// large-magnitude data such as SIFT1M. scale_max is 1.1x the largest
// sampled ||v-centroid||^2 for bucket spread only; any positive value is
// safe, since overflow clamps into the top bucket.
struct raw_euclidean_anchor_t {
  std::vector<double> anchor;
  double scale_max = 0;
};

raw_euclidean_anchor_t build_raw_euclidean_anchor(
    const float_dataset_t& base, uint32_t build_sample)
{
  raw_euclidean_anchor_t out;
  if (base.rows.empty() || base.dimension == 0) {
    return out;
  }
  const size_t vector_count = base.rows.size();
  const size_t sample_limit = std::min<size_t>(
      vector_count, std::max<size_t>(1, build_sample));
  const size_t stride = std::max<size_t>(1, vector_count / sample_limit);
  std::vector<const std::vector<float> *> samples;
  samples.reserve(sample_limit);
  for (size_t i = 0; i < vector_count && samples.size() < sample_limit;
       i += stride) {
    samples.push_back(&base.rows[i]);
  }
  if (samples.empty()) {
    return out;
  }

  out.anchor.assign(base.dimension, 0.0);
  for (const auto *sample : samples) {
    for (uint32_t d = 0; d < base.dimension; ++d) {
      out.anchor[d] += static_cast<double>((*sample)[d]);
    }
  }
  for (uint32_t d = 0; d < base.dimension; ++d) {
    out.anchor[d] /= static_cast<double>(samples.size());
  }

  double max_sq_dist = 0.0;
  for (const auto *sample : samples) {
    double sq_dist = 0.0;
    for (uint32_t d = 0; d < base.dimension; ++d) {
      const double diff = static_cast<double>((*sample)[d]) - out.anchor[d];
      sq_dist += diff * diff;
    }
    max_sq_dist = std::max(max_sq_dist, sq_dist);
  }
  out.scale_max = max_sq_dist > 0 ? max_sq_dist * 1.1 : 1.0;
  return out;
}

pg_lsh::index_config_t requested_config(const options_t& options,
                                        const float_dataset_t& base)
{
  const uint32_t dimension = base.dimension;
  pg_lsh::index_config_t config;
  config.dimension = dimension;
  config.data_type = options.quantize == "int8"
    ? ceph::rados::vector_data_type_int8
    : ceph::rados::vector_data_type_float32;
  config.distance_metric = ceph::rados::vector_distance_metric_euclidean;
  config.k = options.k;
  config.l = options.l;
  config.seed = options.seed;
  config.d = options.d;
  config.partition_num = options.partition_num;
  config.bucket_locators = options.bucket_locators;
  config.placement_mode = options.placement_mode;
  config.hierarchical_scale = options.hierarchical_scale;
  // bucket_locators: routing depends only on the current pg_num, so
  // nothing about the pool's pg_num is pinned in the index.
  config.creation_pg_num = options.bucket_locators ? 0
    : options.partition_num != 0 ? options.partition_num : options.pg_num;
  config.creation_pgp_num = config.creation_pg_num;
  config.distance_bucket_bits = options.distance_bucket_bits;
  config.residual_bits = options.residual_bits;
  if (options.raw_euclidean_geometry) {
    config.placement_layout_version =
      pg_lsh::pg_lsh_layout_version_raw_euclidean_v1;
    config.anchor_mode = pg_lsh::anchor_mode_centroid;
    const auto built = build_raw_euclidean_anchor(
        base, options.pivot_build_sample);
    config.anchor = built.anchor;
    config.raw_distance_scale_max = built.scale_max;
  } else {
    config.anchor_mode = pg_lsh::anchor_mode_random;
    const int r = librados::vector_placement::pg_lsh_v0_random_anchor(
        dimension, options.seed, &config.anchor);
    if (r < 0) config.anchor.clear();
  }
  if (options.placement_mode == pg_lsh::placement_mode_training_derived) {
    // One tree of k levels per LSH table, trained on the same strided
    // --pivot-build-sample rows as the raw-Euclidean anchor. Deterministic
    // in (--base, --base-limit, --pivot-build-sample, --k, --l, --seed),
    // so load and query runs derive identical trees (TD_MODEL digest).
    const size_t vector_count = base.rows.size();
    const size_t sample_limit = std::min<size_t>(
        vector_count, std::max<size_t>(1, options.pivot_build_sample));
    const size_t stride = std::max<size_t>(1, vector_count / sample_limit);
    std::vector<const float *> samples;
    samples.reserve(sample_limit);
    for (size_t i = 0; i < vector_count && samples.size() < sample_limit;
         i += stride) {
      samples.push_back(base.rows[i].data());
    }
    auto trees = std::make_shared<
      std::vector<ceph::rados::vector_pivot_tree::tree_t>>();
    for (uint32_t table = 0; table < options.l; ++table) {
      ceph::rados::vector_pivot_tree::build_params_t params;
      params.levels = librados::vector_placement::pg_lsh_hier_clamp_bits(options.k);
      params.seed = (static_cast<uint64_t>(options.seed) << 16) | table;
      trees->push_back(ceph::rados::vector_pivot_tree::build(
          samples, dimension, params));
    }
    config.trained_trees = std::move(trees);
  }
  return config;
}

void print_training_derived_model(const pg_lsh::index_config_t& config)
{
  if (!config.trained_trees) return;
  uint64_t h = 1469598103934665603ULL;
  uint32_t levels = 0;
  for (const auto& tree : *config.trained_trees) {
    h = (h ^ ceph::rados::vector_pivot_tree::digest(tree)) * 1099511628211ULL;
    levels = tree.levels;
  }
  std::cout << "TD_MODEL tables=" << config.trained_trees->size()
            << " levels=" << levels
            << " digest=" << std::hex << std::setw(16) << std::setfill('0') << h
            << std::dec << std::setfill(' ') << '\n';
}

pg_lsh::query_params_t query_params(const options_t& options)
{
  pg_lsh::query_params_t params;
  params.hamming_radius = options.hamming_radius;
  params.m = options.m;
  params.distance_bucket_radius = options.distance_bucket_radius;
  params.residual_hamming_radius = options.residual_hamming_radius;
  params.probe_limit_per_pg = options.probe_limit_per_pg;
  params.server_side_boundary_search = options.server_side_boundary_search;
  params.tree_boundary_search_budget = options.tree_search_budget;
  params.tree_boundary_search_prune = options.tree_prune;
  params.tree_boundary_search_no_improve_patience =
      options.tree_no_improve_patience;
  params.tree_boundary_search_onode_budget = options.tree_onode_budget;
  params.query_routing.table_count = options.query_table_count;
  params.query_routing.margin_ordered_probes = options.margin_ordered_probes;
  params.band_pg_num = options.bucket_locators ? options.pg_num : 0;
  return params;
}

// The PG a probe was routed to. Partition mode probes several partitions
// per routed PG.
uint32_t routed_pg(const options_t& options, uint32_t pg)
{
  return options.partition_num != 0 ? pg % options.pg_num : pg;
}

bool validate_oid(const options_t& options,
                  const std::string& oid,
                  const std::string& sub_oid)
{
  const auto pg_pos = oid.rfind("/pg_");
  if (pg_pos == std::string::npos) return false;
  const auto after_pg = oid.find('/', pg_pos + 1);
  if (options.routing_mode == "pg-container" ||
      options.routing_mode == "pivot") {
    return sub_oid.empty() && after_pg == std::string::npos;
  }
  return !sub_oid.empty() && after_pg != std::string::npos &&
    oid.substr(after_pg + 1) == sub_oid &&
    oid.find('/', after_pg + 1) == std::string::npos;
}

int validate_locator(const options_t& options, librados::IoCtx& ioctx,
                     uint32_t expected_pg, const std::string& locator)
{
  return pg_lsh::verify_probe_locator(
      ioctx, expected_pg, options.partition_num, locator);
}

// routing-mode=pivot: PG routing by nearest representative (squared L2)
// instead of LSH hashing. Everything downstream of the PG choice is
// unchanged pg-lsh-v0 plumbing; submit_put()/submit_query() only require
// that the locator hashes to the chosen PG.
//
// reps[pg] is the representative for PG `pg`, built by build_pivot_model()
// below and fixed after construction.
struct PivotModel {
  int dim = 0;
  size_t sample_count = 0;
  std::vector<std::vector<float>> reps;
  // Set only under --pivot-place-cap-eps: the PG of every --base row,
  // computed up front by capped_assign() so the cap holds globally. Empty
  // means each row goes to its nearest PG, as before.
  std::vector<uint32_t> placement;
};

// FNV-1a over the raw representative bytes. The pivot model is never
// persisted: the loading process and every querying process recompute it
// from --base/--pg-num/--pivot-build-sample/--pivot-refine-iters. A
// mismatch is silent -- vectors sit in PGs the query never probes and only
// recall collapses -- so every run prints this digest and a sweep asserts
// the query runs match the load run.
inline uint64_t pivot_model_digest(const std::vector<std::vector<float>>& reps)
{
  uint64_t h = 1469598103934665603ULL;
  for (const auto& rep : reps) {
    const auto *bytes = reinterpret_cast<const unsigned char *>(rep.data());
    for (size_t i = 0; i < rep.size() * sizeof(float); ++i) {
      h = (h ^ bytes[i]) * 1099511628211ULL;
    }
  }
  return h;
}

inline float l2_dist_sq(const float *a, const float *b, int dim)
{
  float sum = 0.0f;
  for (int i = 0; i < dim; ++i) {
    const float diff = a[i] - b[i];
    sum += diff * diff;
  }
  return sum;
}

// Runs fn(begin, end) over [0, n) split into contiguous chunks, one per
// hardware thread. Callers only use it for loops whose iterations are
// independent and write disjoint outputs, so the result does not depend on
// the thread count.
template <typename Fn>
void parallel_for(size_t n, Fn&& fn)
{
  const size_t threads = std::max<size_t>(
      1, std::min<size_t>(std::thread::hardware_concurrency(), n / 64));
  if (threads <= 1) {
    fn(size_t{0}, n);
    return;
  }
  const size_t chunk = (n + threads - 1) / threads;
  std::vector<std::thread> workers;
  workers.reserve(threads);
  for (size_t t = 0; t < threads; ++t) {
    const size_t begin = t * chunk;
    const size_t end = std::min(n, begin + chunk);
    if (begin >= end) break;
    workers.emplace_back([&fn, begin, end] { fn(begin, end); });
  }
  for (auto& w : workers) w.join();
}

// Capacity-constrained assignment for balanced Lloyd and capped placement.
// Every point keeps its `topn` nearest representatives (parallel); then,
// sequentially and in order of increasing nearest distance (ties by index),
// each point takes the nearest representative that still has room under
// cap = ceil((1+eps) * points / k). A point whose topn are all full goes to
// the least-filled of them, so the cap is soft only in that corner. Order and
// tie rules are fixed, so the result is deterministic.
template <typename RowFn>
void capped_assign(size_t n, RowFn&& row, const PivotModel& model, double eps,
                   std::vector<size_t> *assignment, std::vector<float> *dist)
{
  const size_t k = model.reps.size();
  const size_t topn = std::min<size_t>(k, 64);
  const size_t cap = static_cast<size_t>(
      std::ceil((1.0 + eps) * static_cast<double>(n) / static_cast<double>(k)));
  std::vector<uint32_t> cand_pg(n * topn);
  std::vector<float> cand_d(n * topn);
  parallel_for(n, [&](size_t b, size_t e) {
    std::vector<std::pair<float, uint32_t>> all(k);
    for (size_t i = b; i < e; ++i) {
      const float *v = row(i);
      for (size_t p = 0; p < k; ++p) {
        all[p] = {l2_dist_sq(v, model.reps[p].data(), model.dim),
                  static_cast<uint32_t>(p)};
      }
      std::partial_sort(all.begin(), all.begin() + topn, all.end());
      for (size_t t = 0; t < topn; ++t) {
        cand_d[i * topn + t] = all[t].first;
        cand_pg[i * topn + t] = all[t].second;
      }
    }
  });
  std::vector<size_t> order(n);
  std::iota(order.begin(), order.end(), size_t{0});
  std::sort(order.begin(), order.end(), [&](size_t a, size_t b) {
    if (cand_d[a * topn] != cand_d[b * topn]) return cand_d[a * topn] < cand_d[b * topn];
    return a < b;
  });
  std::vector<size_t> fill(k, 0);
  for (const size_t i : order) {
    size_t pick = topn;
    for (size_t t = 0; t < topn; ++t) {
      if (fill[cand_pg[i * topn + t]] < cap) { pick = t; break; }
    }
    if (pick == topn) {
      pick = 0;
      for (size_t t = 1; t < topn; ++t) {
        if (fill[cand_pg[i * topn + t]] < fill[cand_pg[i * topn + pick]]) pick = t;
      }
    }
    const uint32_t pg = cand_pg[i * topn + pick];
    ++fill[pg];
    (*assignment)[i] = pg;
    (*dist)[i] = cand_d[i * topn + pick];
  }
}

void balanced_assign(const std::vector<const std::vector<float> *>& samples,
                     const PivotModel& model, double eps,
                     std::vector<size_t> *assignment, std::vector<float> *dist)
{
  capped_assign(samples.size(), [&](size_t i) { return samples[i]->data(); },
                model, eps, assignment, dist);
}

// Deterministic stride sampling plus farthest-first traversal: the first
// sample is the first representative, and each next representative is the
// sample farthest from those already chosen, ties going to the smallest
// index.
//
// With refine_iters > 0 the farthest-first representatives are then refined
// by that many Lloyd (k-means) passes -- see the loop at the end. Farthest-
// first only picks well-separated real samples, which are by construction
// extreme points rather than cluster centres; Lloyd moves each one to the
// mean of the samples it actually owns. refine_iters == 0 reproduces the
// unrefined model bit for bit.
//
// spherical rescales each refined mean to the sample's mean norm. On
// unit-norm data a plain mean has norm < 1, and since
// |q - c|^2 = |q|^2 + |c|^2 - 2 q.c, the smaller |c| of a diffuse cluster
// leaks into the ranking; spherical removes that term.
//
// The per-sample distance loops run in parallel; every reduction (argmax,
// sums, SSE) stays sequential in sample order, so the model is the same bit
// for bit whatever the thread count.
PivotModel build_pivot_model(const float_dataset_t& base,
                             uint32_t pg_num,
                             uint32_t pivot_build_sample,
                             uint32_t refine_iters,
                             bool spherical,
                             double balance_eps = 0.0)
{
  PivotModel model;
  model.dim = static_cast<int>(base.dimension);
  if (base.rows.empty() || pg_num == 0 || base.dimension == 0) {
    return model;
  }

  const size_t vector_count = base.rows.size();
  const size_t sample_limit = std::min<size_t>(
      vector_count, std::max<size_t>(1, pivot_build_sample));
  const size_t stride = std::max<size_t>(1, vector_count / sample_limit);
  std::vector<const std::vector<float> *> samples;
  samples.reserve(sample_limit);
  for (size_t i = 0; i < vector_count && samples.size() < sample_limit;
       i += stride) {
    samples.push_back(&base.rows[i]);
  }
  if (samples.empty()) {
    return model;
  }
  model.sample_count = samples.size();

  const size_t pivot_count = std::min<size_t>(pg_num, samples.size());
  model.reps.reserve(pivot_count);

  std::vector<float> min_dist(samples.size(), std::numeric_limits<float>::max());
  model.reps.push_back(*samples[0]);
  parallel_for(samples.size(), [&](size_t b, size_t e) {
    for (size_t i = b; i < e; ++i) {
      min_dist[i] = l2_dist_sq(samples[i]->data(), samples[0]->data(), model.dim);
    }
  });
  min_dist[0] = 0.0f;

  while (model.reps.size() < pivot_count) {
    size_t best_idx = 0;
    float best_dist = -1.0f;
    for (size_t i = 0; i < samples.size(); ++i) {
      if (min_dist[i] > best_dist) {
        best_dist = min_dist[i];
        best_idx = i;
      }
    }
    if (best_dist <= 0.0f) {
      break;
    }
    model.reps.push_back(*samples[best_idx]);
    min_dist[best_idx] = 0.0f;
    const float *chosen = samples[best_idx]->data();
    parallel_for(samples.size(), [&](size_t b, size_t e) {
      for (size_t i = b; i < e; ++i) {
        const float d2 = l2_dist_sq(samples[i]->data(), chosen, model.dim);
        if (d2 < min_dist[i]) {
          min_dist[i] = d2;
        }
      }
    });
  }

  if (refine_iters == 0) {
    return model;
  }

  double target_norm = 0.0;
  if (spherical) {
    for (const auto *row : samples) {
      double sq = 0.0;
      for (int j = 0; j < model.dim; ++j) sq += double((*row)[j]) * (*row)[j];
      target_norm += std::sqrt(sq);
    }
    target_norm /= static_cast<double>(samples.size());
  }

  // Lloyd refinement. Pass i assigns every sample to its nearest
  // representative (ties to the smaller PG id, the same rule
  // pivot_rank_pgs() uses) and then replaces each representative with the
  // mean of its cluster. One extra assignment-only pass runs at the end so
  // the log reports the final model's SSE and cluster sizes rather than the
  // second-to-last model's.
  //
  // Sums are accumulated in double in sample order: the model is recomputed
  // independently by the load and the query processes, so the result has to
  // be bit-reproducible, not merely close.
  const size_t k = model.reps.size();
  std::vector<size_t> assignment(samples.size(), 0);
  std::vector<float> assigned_dist(samples.size(), 0.0f);
  std::vector<size_t> count(k, 0);
  std::vector<double> sum(k * static_cast<size_t>(model.dim), 0.0);
  for (uint32_t it = 0; it <= refine_iters; ++it) {
    std::fill(count.begin(), count.end(), 0);
    std::fill(sum.begin(), sum.end(), 0.0);
    parallel_for(samples.size(), [&](size_t b, size_t e) {
      for (size_t i = b; i < e; ++i) {
        size_t best = 0;
        float best_d = std::numeric_limits<float>::max();
        for (size_t p = 0; p < k; ++p) {
          const float d2 = l2_dist_sq(
              samples[i]->data(), model.reps[p].data(), model.dim);
          if (d2 < best_d) {
            best_d = d2;
            best = p;
          }
        }
        assignment[i] = best;
        assigned_dist[i] = best_d;
      }
    });
    if (balance_eps > 0.0) {
      balanced_assign(samples, model, balance_eps, &assignment, &assigned_dist);
    }
    double sse = 0.0;
    for (size_t i = 0; i < samples.size(); ++i) {
      const size_t best = assignment[i];
      sse += assigned_dist[i];
      ++count[best];
      const float *row = samples[i]->data();
      double *acc = &sum[best * static_cast<size_t>(model.dim)];
      for (int j = 0; j < model.dim; ++j) {
        acc[j] += row[j];
      }
    }

    size_t empty = 0;
    size_t min_cluster = std::numeric_limits<size_t>::max();
    size_t max_cluster = 0;
    for (size_t p = 0; p < k; ++p) {
      if (count[p] == 0) ++empty;
      min_cluster = std::min(min_cluster, count[p]);
      max_cluster = std::max(max_cluster, count[p]);
    }

    // it == 0 describes the raw farthest-first model, so the log always
    // carries the baseline it is being compared against; the last pass is
    // stats-only and leaves the returned model untouched.
    double shift = 0.0;
    if (it < refine_iters) {
      for (size_t p = 0; p < k; ++p) {
        // An empty cluster has no mean: keep the previous representative so
        // that reps.size() stays pg_num and the model stays well defined.
        if (count[p] == 0) continue;
        const double *acc = &sum[p * static_cast<size_t>(model.dim)];
        double scale = 1.0;
        if (spherical) {
          double sq = 0.0;
          for (int j = 0; j < model.dim; ++j) {
            const double m = acc[j] / count[p];
            sq += m * m;
          }
          if (sq > 0.0) scale = target_norm / std::sqrt(sq);
        }
        double moved = 0.0;
        for (int j = 0; j < model.dim; ++j) {
          const float next = static_cast<float>(acc[j] / count[p] * scale);
          const double diff = next - model.reps[p][j];
          moved += diff * diff;
          model.reps[p][j] = next;
        }
        shift += std::sqrt(moved);
      }
      shift /= static_cast<double>(k);
    }

    std::cout << std::fixed << std::setprecision(6)
              << "PIVOT_REFINE iter=" << it
              << " sse=" << sse
              << " mean_sq_err=" << (sse / static_cast<double>(samples.size()))
              << " empty_clusters=" << empty
              << " min_cluster=" << min_cluster
              << " max_cluster=" << max_cluster
              << " skew_max_over_mean="
              << (static_cast<double>(max_cluster) * static_cast<double>(k) /
                  static_cast<double>(samples.size()))
              << " mean_rep_shift=" << shift << '\n';
  }

  return model;
}

// Ranks every representative by squared L2 distance to `vec` and returns up
// to `keep_n` PG ids, nearest first (keep_n is pivot_probe_budget; callers
// further slice the first --d (write) or --m (probe) of the result). Ties
// broken by smaller PG id for determinism.
std::vector<uint32_t> pivot_rank_pgs(const float *vec, int dim,
                                     const PivotModel& model, uint32_t keep_n)
{
  struct dist_pg_t { float dist; uint32_t pg; };
  std::vector<dist_pg_t> ranked;
  ranked.reserve(model.reps.size());
  for (size_t pg = 0; pg < model.reps.size(); ++pg) {
    ranked.push_back(
        {l2_dist_sq(vec, model.reps[pg].data(), dim), static_cast<uint32_t>(pg)});
  }
  std::sort(ranked.begin(), ranked.end(),
      [](const dist_pg_t& a, const dist_pg_t& b) {
        if (a.dist != b.dist) return a.dist < b.dist;
        return a.pg < b.pg;
      });
  const size_t keep = std::min<size_t>(keep_n, ranked.size());
  std::vector<uint32_t> out(keep);
  for (size_t i = 0; i < keep; ++i) {
    out[i] = ranked[i].pg;
  }
  return out;
}

// Builds put_target_t/query_probe_t entries for pivot-selected PGs by
// reusing the exact same OID/locator/placement_key construction as
void expand_full_pg_scan(const options_t& options,
                         std::vector<pg_lsh::query_probe_t> *probes);

// pg_lsh::build_put_targets()/build_query_probes() (vector_placement.h) --
// only the PG selection differs. One-object-per-PG (empty sub_oid), same as
// routing-mode=pg-container.
int pivot_put_targets(const options_t& options, const ceph::bufferlist& vector_bl,
                      const std::vector<uint32_t>& pgs,
                      pg_lsh::locator_cache_t *locator_cache,
                      std::vector<pg_lsh::put_target_t> *targets)
{
  targets->clear();
  targets->reserve(pgs.size());
  const std::string vector_hash =
    librados::vector_placement::hash_v0_vector_hash(vector_bl);
  for (const uint32_t pg : pgs) {
    std::string locator_key;
    const int r = locator_cache->locator_for_pg(pg, &locator_key);
    if (r < 0) return r;
    targets->push_back({
      pg,
      librados::vector_placement::make_pg_lsh_v0_oid(
          options.bucket, options.index, pg, ""),
      std::move(locator_key),
      librados::vector_placement::pg_lsh_v0_placement_key(pg),
      vector_hash,
      "",
    });
  }
  return 0;
}

int pivot_query_probes(const options_t& options,
                       const std::vector<uint32_t>& pgs,
                       pg_lsh::locator_cache_t *locator_cache,
                       std::vector<pg_lsh::query_probe_t> *probes)
{
  probes->clear();
  probes->reserve(pgs.size());
  for (const uint32_t pg : pgs) {
    std::string locator_key;
    const int r = locator_cache->locator_for_pg(pg, &locator_key);
    if (r < 0) return r;
    probes->push_back({
      pg,
      0,
      0,
      librados::vector_placement::make_pg_lsh_v0_oid(
          options.bucket, options.index, pg, ""),
      std::move(locator_key),
      librados::vector_placement::pg_lsh_v0_placement_key(pg),
      "",
    });
  }
  return 0;
}

// Sequential at load_concurrency=1. Each worker owns its IoCtx and
// locator_cache and shares the precomputed locator_state; rows are handed
// out by an atomic ticket counter, so above concurrency 1 the row that
// "first_oid" reports is whichever finished first. Any failure raises
// "stop" and every worker winds down.
load_result_t load_vectors(const options_t& options,
                           const float_dataset_t& base,
                           librados::Rados& cluster,
                           std::shared_ptr<pg_lsh::locator_state_t> locator_state,
                           const pg_lsh::index_config_t& config,
                           const PivotModel *pivot_model = nullptr)
{
  load_result_t result;
  const auto begin = clock_type::now();
  std::atomic<uint64_t> next{0};
  std::atomic<bool> stop{false};
  std::atomic<int> first_error{0};

  struct worker_result_t {
    load_result_t result;
  };
  std::vector<worker_result_t> worker_results(options.load_concurrency);
  std::vector<std::thread> workers;
  workers.reserve(options.load_concurrency);

  for (uint32_t worker_id = 0; worker_id < options.load_concurrency; ++worker_id) {
    workers.emplace_back([&, worker_id] {
      auto& local = worker_results[worker_id].result;
      librados::IoCtx ioctx;
      int r = cluster.ioctx_create(options.pool.c_str(), ioctx);
      if (r < 0) {
        ++local.failures;
        local.first_error = r;
        int expected = 0;
        first_error.compare_exchange_strong(expected, r);
        stop.store(true);
        return;
      }
      pg_lsh::locator_cache_t locator_cache(
          &ioctx, pool_info(options), options.pool, options.bucket,
          options.index, locator_state, options.partition_num,
      options.bucket_locators, options.k);
      r = locator_cache.precompute_all();
      if (r < 0) {
        ++local.failures;
        local.first_error = r;
        int expected = 0;
        first_error.compare_exchange_strong(expected, r);
        stop.store(true);
        return;
      }

      ceph::bufferlist metadata;
      while (!stop.load(std::memory_order_relaxed)) {
        const uint64_t row = next.fetch_add(1, std::memory_order_relaxed);
        if (row >= base.rows.size()) break;

        const auto vector_bl = vector_buffer_typed(base.rows[row], config.data_type,
                                          options.quantize_scale);
        std::vector<pg_lsh::put_target_t> targets;
        int pr;
        if (options.routing_mode == "pivot") {
          const auto ranked = pivot_rank_pgs(
              base.rows[row].data(), pivot_model->dim, *pivot_model,
              options.pivot_probe_budget);
          std::vector<uint32_t> write_pgs(
              ranked.begin(),
              ranked.begin() + std::min<size_t>(options.d, ranked.size()));
          if (!pivot_model->placement.empty()) {
            write_pgs.assign(1, pivot_model->placement[row]);
          }
          pr = pivot_put_targets(
              options, vector_bl, write_pgs, &locator_cache, &targets);
        } else {
          pr = pg_lsh::build_put_targets(
              options.bucket, options.index, vector_buffer(base.rows[row]),
              placement_config(config), pool_info(options), &locator_cache,
              &targets);
        }
        if (options.write_rank_begin > 0 && pr >= 0) {
          if (options.write_rank_begin >= targets.size()) {
            targets.clear();
            ++local.rank_skipped;
            ++local.completed;
            continue;
          }
          targets.erase(targets.begin(),
                        targets.begin() + options.write_rank_begin);
        }
        local.put_targets += targets.size();
        // d>1 write fanout: build_put_targets() already dedups to at most
        // config.d unique PGs, so fewer targets than requested (PG
        // collisions between LSH tables) is expected and reported via
        // actual_put_targets_per_vector, not treated as a failure -- only
        // an empty set or a malformed individual target is.
        bool targets_ok = pr >= 0 && !targets.empty();
        if (targets_ok) {
          for (const auto& target : targets) {
            if (!validate_oid(options, target.oid.name, target.sub_oid_name) ||
                validate_locator(options, ioctx, target.pg, target.locator_key) < 0) {
              targets_ok = false;
              break;
            }
          }
        }
        if (!targets_ok) {
          ++local.failures;
          ++local.target_violations;
          local.first_error = pr < 0 ? pr : -EINVAL;
          int expected = 0;
          first_error.compare_exchange_strong(expected, local.first_error);
          stop.store(true);
          break;
        }
        if (local.first_oid.empty()) local.first_oid = targets.front().oid.name;
        for (const auto& target : targets) {
          local.vector_oids.insert(target.oid.name);
        }

        bool put_failed = false;
        int put_error = 0;
        for (const auto& target : targets) {
          ceph::rados::put_vector_request_t req;
          req.bucket_name = options.bucket;
          req.index_name = options.index;
          req.key = vector_key(options, row);
          req.data_type = config.data_type;
          req.distance_metric = config.distance_metric;
          req.dimension = config.dimension;
          req.vector_data = vector_bl;
          req.metadata = metadata;
          pr = pg_lsh::put_vector(ioctx, target, std::move(req));
          if (pr < 0) {
            put_failed = true;
            put_error = pr;
            break;
          }
        }
        if (put_failed) {
          ++local.failures;
          local.first_error = put_error;
          int expected = 0;
          first_error.compare_exchange_strong(expected, put_error);
          stop.store(true);
          break;
        }
        ++local.completed;
      }
    });
  }
  for (auto& worker : workers) worker.join();
  result.seconds = std::chrono::duration<double>(clock_type::now() - begin).count();
  result.first_error = first_error.load();
  for (auto& worker : worker_results) {
    auto& local = worker.result;
    result.completed += local.completed;
    result.failures += local.failures;
    result.put_targets += local.put_targets;
    result.rank_skipped += local.rank_skipped;
    result.target_violations += local.target_violations;
    result.vector_oids.insert(local.vector_oids.begin(), local.vector_oids.end());
    if (result.first_oid.empty()) result.first_oid = local.first_oid;
  }
  return result;
}

double percentile(std::vector<double> values, double fraction)
{
  if (values.empty()) return 0;
  std::sort(values.begin(), values.end());
  const size_t index = std::min(
      values.size() - 1,
      static_cast<size_t>(std::ceil(fraction * values.size())) - 1);
  return values[index];
}

bool sorted_distances(const std::vector<ceph::rados::query_vectors_result_entry_t>& entries)
{
  for (size_t i = 0; i < entries.size(); ++i) {
    if (!std::isfinite(entries[i].distance) ||
        (i != 0 && entries[i - 1].distance > entries[i].distance)) {
      return false;
    }
  }
  return true;
}

// --destination-pg-log verification sink: records which real PG each of
// the first `cap` requests actually resolved to, so a curated
// --query-index-list workload's PG-fanout can be proven from the live run.
struct dest_pg_log_t {
  std::mutex lock;
  std::ofstream out;
  uint64_t cap = 500;
  uint64_t written = 0;

  void record(uint64_t ticket, size_t query_index, uint32_t pg)
  {
    std::lock_guard<std::mutex> locker(lock);
    if (written >= cap) return;
    out << ticket << ',' << query_index << ',' << pg << '\n';
    ++written;
  }
};

query_result_t run_queries(
    const options_t& options,
    const float_dataset_t& queries,
    const ivec_dataset_t *groundtruth,
    const pg_lsh::index_config_t& config,
    librados::Rados& cluster,
    std::shared_ptr<pg_lsh::locator_state_t> locator_state,
    uint32_t rounds,
    uint64_t min_queries,
    uint32_t min_seconds,
    bool measure,
    dest_pg_log_t *dest_log = nullptr)
{
  query_result_t result;
  const uint64_t fixed_total =
    static_cast<uint64_t>(queries.rows.size()) * rounds;
  std::atomic<uint64_t> next{0};
  std::atomic<bool> stop{false};
  std::atomic<int> first_error{0};
  std::mutex first_oid_lock;
  const auto begin = clock_type::now();

  struct worker_result_t {
    query_result_t result;
  };
  std::vector<worker_result_t> worker_results(options.query_concurrency);
  std::vector<std::thread> workers;
  workers.reserve(options.query_concurrency);

  for (uint32_t worker_id = 0; worker_id < options.query_concurrency; ++worker_id) {
    workers.emplace_back([&, worker_id] {
      auto& local = worker_results[worker_id].result;
      librados::IoCtx ioctx;
      int r = cluster.ioctx_create(options.pool.c_str(), ioctx);
      if (r < 0) {
        local.failures = 1;
        local.first_error = r;
        int expected = 0;
        first_error.compare_exchange_strong(expected, r);
        stop.store(true);
        return;
      }
      pg_lsh::locator_cache_t cache(
          &ioctx, pool_info(options), options.pool, options.bucket,
          options.index, locator_state, options.partition_num,
      options.bucket_locators, options.k);
      r = cache.precompute_all();
      if (r < 0) {
        local.failures = 1;
        local.first_error = r;
        int expected = 0;
        first_error.compare_exchange_strong(expected, r);
        stop.store(true);
        return;
      }

      while (!stop.load(std::memory_order_relaxed)) {
        const uint64_t ticket = next.fetch_add(1, std::memory_order_relaxed);
        if (min_queries == 0 && min_seconds == 0) {
          if (ticket >= fixed_total) break;
        } else if (ticket >= min_queries) {
          const double elapsed = std::chrono::duration<double>(
              clock_type::now() - begin).count();
          if (elapsed >= min_seconds) break;
        }
        const size_t query_index = ticket % queries.rows.size();
        const auto query_bl = vector_buffer_typed(queries.rows[query_index],
                                           config.data_type, options.quantize_scale);
        std::vector<pg_lsh::query_probe_t> probes;
        uint64_t generated_groups = 0;
        r = pg_lsh::build_query_probes(
            options.bucket, options.index,
            vector_buffer(queries.rows[query_index]),
            placement_config(config),
            query_params(options), pool_info(options), &cache, &probes,
            &generated_groups);
        std::unordered_set<uint32_t> unique_pgs;
        for (const auto& probe : probes) unique_pgs.insert(probe.pg);
        if (r < 0 || generated_groups != config.l || probes.size() != 1 ||
            unique_pgs.size() != 1 ||
            !validate_oid(options, probes.front().oid.name,
                          probes.front().sub_oid_name) ||
            validate_locator(options, ioctx, probes.front().pg,
                             probes.front().locator_key) < 0) {
          ++local.failures;
          local.first_error = r < 0 ? r : -EINVAL;
          int expected = 0;
          first_error.compare_exchange_strong(expected, local.first_error);
          stop.store(true);
          break;
        }
        ++local.pg_probes;
        ++local.object_probes;
        if (local.first_oid.empty()) local.first_oid = probes.front().oid.name;
        if (dest_log != nullptr) {
          dest_log->record(ticket, query_index, probes.front().pg);
        }

        ceph::rados::query_vectors_request_t req;
        req.bucket_name = options.bucket;
        req.index_name = options.index;
        req.data_type = config.data_type;
        req.distance_metric = config.distance_metric;
        req.dimension = config.dimension;
        req.local_top_k = options.top_k;
        req.query_vector = query_bl;
        pg_lsh::apply_boundary_search_config(config, query_params(options), &req);
        pg_lsh::apply_tree_boundary_search_config(config, query_params(options), &req);
        ceph::bufferlist reply;
        int op_rval = 0;
        const auto query_begin = clock_type::now();
        r = pg_lsh::query_sync(
            ioctx, probes.front(), std::move(req), &reply, &op_rval);
        const auto query_end = clock_type::now();
        if (r == -ENOENT || op_rval == -ENOENT) {
          ++local.missing_objects;
          ++local.empty_results;
          ++local.completed;
          if (measure) {
            local.latency_us.push_back(std::chrono::duration<double, std::micro>(
                query_end - query_begin).count());
          }
          continue;
        }
        if (r < 0 || op_rval < 0 || reply.length() == 0) {
          ++local.failures;
          local.first_error = r < 0 ? r : (op_rval < 0 ? op_rval : -EIO);
          int expected = 0;
          first_error.compare_exchange_strong(expected, local.first_error);
          stop.store(true);
          break;
        }

        ceph::rados::query_vectors_result_t partial;
        try {
          auto p = reply.cbegin();
          decode(partial, p);
          if (!p.end()) throw ceph::buffer::malformed_input("trailing reply");
        } catch (const ceph::buffer::error&) {
          ++local.failures;
          local.first_error = -EIO;
          int expected = 0;
          first_error.compare_exchange_strong(expected, -EIO);
          stop.store(true);
          break;
        }

        if (partial.entries.empty()) ++local.empty_results;
        if (!sorted_distances(partial.entries)) ++local.unsorted_results;
        if (partial.local_matching_entries == 0 ||
            partial.local_distance_computations == 0) {
          ++local.zero_counter_results;
        }
        local.returned_entries += partial.entries.size();
        local.matching_records += partial.local_matching_entries;
        local.distance_computations += partial.local_distance_computations;
        if (groundtruth != nullptr && query_index < groundtruth->rows.size()) {
          const auto& truth = groundtruth->rows[query_index];
          const size_t truth_k = std::min<size_t>(options.top_k, truth.size());
          std::set<std::string> expected;
          for (size_t i = 0; i < truth_k; ++i) {
            expected.insert(expected_entry_id(options, truth[i]));
          }
          for (const auto& entry : partial.entries) {
            if (expected.erase(entry.entry_id) != 0) ++local.recall_hits;
          }
          local.recall_denominator += truth_k;
        }
        ++local.completed;
        if (measure) {
          local.latency_us.push_back(std::chrono::duration<double, std::micro>(
              query_end - query_begin).count());
        }
      }
    });
  }
  for (auto& worker : workers) worker.join();
  result.seconds = std::chrono::duration<double>(clock_type::now() - begin).count();
  result.first_error = first_error.load();
  for (auto& worker : worker_results) {
    auto& local = worker.result;
    result.completed += local.completed;
    result.failures += local.failures;
    result.missing_objects += local.missing_objects;
    result.empty_results += local.empty_results;
    result.unsorted_results += local.unsorted_results;
    result.zero_counter_results += local.zero_counter_results;
    result.pg_probes += local.pg_probes;
    result.object_probes += local.object_probes;
    result.matching_records += local.matching_records;
    result.distance_computations += local.distance_computations;
    result.returned_entries += local.returned_entries;
    result.recall_hits += local.recall_hits;
    result.recall_denominator += local.recall_denominator;
    result.rerank_unmapped += local.rerank_unmapped;
    result.latency_us.insert(result.latency_us.end(),
                             local.latency_us.begin(), local.latency_us.end());
    if (result.first_oid.empty() && !local.first_oid.empty()) {
      result.first_oid = local.first_oid;
    }
  }
  return result;
}

double average(uint64_t value, uint64_t denominator)
{
  return denominator == 0 ? 0.0 :
    static_cast<double>(value) / static_cast<double>(denominator);
}

// One row per (query, rank) of a completed logical query's merged top-k.
// Fixed precision so two runs' files are byte-comparable.
void write_query_results_csv(
    std::ofstream *out,
    std::mutex& lock,
    size_t query_index,
    const std::vector<ceph::rados::query_vectors_result_entry_t>& merged)
{
  std::ostringstream rows;
  rows << std::fixed << std::setprecision(6);
  for (size_t i = 0; i < merged.size(); ++i) {
    rows << query_index << ',' << i << ',' << merged[i].entry_id << ','
         << merged[i].distance << '\n';
  }
  std::lock_guard<std::mutex> locker(lock);
  *out << rows.str();
}

struct fanout_query_result_t {
  uint64_t completed = 0;
  uint64_t failures = 0;
  uint64_t missing_objects = 0;
  // Entries whose logical key (query_vectors_result_entry_t::key) appeared
  // in more than one probe's reply for the same logical query. --d 1 means
  // every stored vector lives in exactly one physical object, so this is
  // expected to be 0 under these conditions -- reported, not assumed.
  uint64_t duplicate_entries = 0;
  uint64_t returned_entries = 0;
  // Summed across every probe of every completed logical query (i.e. the
  // per-logical-query TOTAL, summed again across queries) -- divide by
  // completed*probe_count for the per-PG average, or by completed alone
  // for the per-logical-query-total average. Never treat this as a
  // per-request count the way single-probe query_result_t's fields are.
  uint64_t probe_matching_records = 0;
  uint64_t probe_distance_computations = 0;
  uint64_t recall_hits = 0;
  uint64_t recall_denominator = 0;
  // Shortlist entries whose base row could not be recovered, so they
  // kept their server-side rank instead of being rescored. Expected 0.
  uint64_t rerank_unmapped = 0;
  // RADOS requests actually put on the wire, and QUERY_VECTORS ops carried
  // by them. Equal without --query-batch-size (one op per request); with
  // it, ops stays the same -- routing and fan-out are untouched -- and
  // requests is what drops.
  uint64_t osd_requests = 0;
  uint64_t osd_query_ops = 0;
  int first_error = 0;
  std::string first_oid;
  double seconds = 0;
  std::vector<double> latency_us;
  // Pipeline occupancy: without these --io-depth is a knob with no way to
  // tell whether the window ever filled. queue_wait is time a query spent
  // with an issuable probe and no free slot; active is the rest of its
  // end-to-end. inflight_integral/pipeline_wall give the average depth.
  uint64_t queue_wait_us_sum = 0;
  uint64_t active_us_sum = 0;
  double inflight_integral_us = 0;
  double pipeline_wall_us = 0;
  size_t max_inflight = 0;
  // Summed across every probe of every completed logical query that came
  // back with a query_vectors_tree_stats_t, i.e. every tree-search probe.
  uint64_t tree_stats_probes = 0;
  // Application payload of every probe reply, and the raw-vector part of it.
  uint64_t reply_bytes = 0;
  uint64_t reply_vector_bytes = 0;
  uint64_t tree_primary_candidate_count = 0;
  uint64_t tree_expanded_subtree_count = 0;
  uint64_t tree_pruned_distance_count = 0;
  uint64_t tree_pruned_residual_count = 0;
  uint64_t tree_containment_violations = 0;
  uint64_t tree_opened_onode_count = 0;
  uint64_t tree_candidates_seen = 0;
  // Selective fan-out (--selective). final_m is the cumulative PG count the
  // query stopped at, which under the default stop_mode_t::never is always
  // the schedule's last stage. stages_sum over completed is the average
  // number of sequential round-trips per query, the latency this trades
  // candidates for.
  uint64_t selective_final_m_sum = 0;
  uint64_t selective_stages_sum = 0;
  std::map<uint32_t, uint64_t> selective_final_m_histogram;
};

struct fanout_probe_outcome_t {
  uint32_t pg = 0;
  std::string sub_oid;
  int r = 0;
  int op_rval = 0;
  ceph::bufferlist reply;
  // When this probe became issuable, versus when a slot was actually free
  // for it: submit_us - ready_us is its wait for a slot.
  double ready_us = 0;
  double submit_us = 0;
  double complete_us = 0;
  uint64_t matching_records = 0;
  uint64_t distance_computations = 0;
};

// One logical query resolves through config.m to m distinct candidate PGs,
// i.e. multi-probe LSH fan-out rather than m independent queries.
// fanout_mode picks the dispatch: "sequential" issues and waits one at a
// time, "parallel" submits all m before waiting on any. Both resolve the
// same probe set, since build_query_probes() runs once up front.
// Runs "<hook> <phase>" for --measure-hook; a failing hook aborts the run,
// since the byte counters it brackets would otherwise be silently wrong.
void run_measure_hook(const std::string& hook, const char *phase)
{
  const std::string cmd = hook + " " + phase;
  if (std::system(cmd.c_str()) != 0) {
    std::cerr << "error: measure hook failed: " << cmd << '\n';
    std::exit(3);
  }
}

fanout_query_result_t run_fanout_queries(
    const options_t& options,
    const float_dataset_t& queries,
    const ivec_dataset_t *groundtruth,
    const pg_lsh::index_config_t& config,
    librados::Rados& cluster,
    std::shared_ptr<pg_lsh::locator_state_t> locator_state,
    uint32_t rounds,
    uint64_t min_queries,
    uint32_t min_seconds,
    bool measure,
    std::ofstream *fanout_log,
    const sf::policy_t& policy,
    std::ofstream *selective_csv = nullptr,
    const PivotModel *pivot_model = nullptr,
    std::ofstream *results_csv = nullptr)
{
  fanout_query_result_t result;
  const uint64_t fixed_total =
    static_cast<uint64_t>(queries.rows.size()) * rounds;
  std::atomic<uint64_t> next{0};
  std::atomic<bool> stop{false};
  std::atomic<int> first_error{0};
  std::mutex log_lock;
  const auto begin = clock_type::now();

  struct worker_result_t { fanout_query_result_t result; };
  std::vector<worker_result_t> worker_results(options.query_concurrency);
  std::vector<std::thread> workers;
  workers.reserve(options.query_concurrency);

  for (uint32_t worker_id = 0; worker_id < options.query_concurrency; ++worker_id) {
    workers.emplace_back([&, worker_id] {
      auto& local = worker_results[worker_id].result;
      librados::IoCtx ioctx;
      int r = cluster.ioctx_create(options.pool.c_str(), ioctx);
      if (r < 0) {
        local.failures = 1;
        local.first_error = r;
        int expected = 0;
        first_error.compare_exchange_strong(expected, r);
        stop.store(true);
        return;
      }
      pg_lsh::locator_cache_t cache(
          &ioctx, pool_info(options), options.pool, options.bucket,
          options.index, locator_state, options.partition_num,
      options.bucket_locators, options.k);
      r = cache.precompute_all();
      if (r < 0) {
        local.failures = 1;
        local.first_error = r;
        int expected = 0;
        first_error.compare_exchange_strong(expected, r);
        stop.store(true);
        return;
      }
      const bool run_hook = measure && !options.measure_hook.empty();
      if (run_hook) run_measure_hook(options.measure_hook, "begin");

      // Slots are refilled across several in-flight logical queries, not
      // within one: the m probes of a single query all target distinct PGs,
      // so a per-query submit-all/wait-all barrier holds every PG at depth 1
      // and leaves the OSD idle during that query's routing and merge.
      //
      // At --io-depth 0 claim_slot() grows slots without a bound, so the
      // loop submits a whole stage before waiting on it. That is the old
      // behaviour, which is why there is only one dispatch path here.
      const bool sequential_mode = options.fanout_mode == "sequential";
      const bool bounded_depth = options.io_depth > 0;
      const size_t query_slot_count = bounded_depth ? options.io_depth : 1;

      struct probe_slot_t {
        size_t query_slot = 0;
        size_t probe_index = 0;
        size_t outcome_index = 0;
        // Recreated per op: ObjectOperation accumulates, and routed_ioctx
        // must outlive the op, so a slot never reuses one.
        std::unique_ptr<pg_lsh::query_op_state_t> op_state;
        librados::AioCompletion *completion = nullptr;
        // Stable storage for the completion callback's argument.
        void *queue = nullptr;
        size_t self = 0;
      };
      struct query_slot_t {
        bool active = false;
        uint64_t ticket = 0;
        size_t query_index = 0;
        clock_type::time_point query_begin{};
        ceph::bufferlist query_bl;
        std::vector<pg_lsh::query_probe_t> probes;
        std::vector<std::pair<size_t, size_t>> pg_probe_ranges;
        size_t distinct_pgs = 0;
        uint32_t available_pgs = 0;
        std::optional<sf::topk_accumulator_t<
            ceph::rados::query_vectors_result_entry_t>> accumulator;
        std::vector<fanout_probe_outcome_t> outcomes;
        std::vector<fanout_probe_outcome_t> stage_outcomes;
        std::vector<sf::stage_record_t> stage_records;
        std::optional<float> tau_prev;
        size_t stage = 0;
        uint32_t stage_range_begin = 0;
        uint32_t stage_range_end = 0;
        size_t stage_probe_begin = 0;
        size_t next_probe = 0;
        size_t probe_end = 0;
        size_t outstanding = 0;
        double stage_begin_us = 0;
        double stage_end_us = 0;
        bool no_stages = false;
        // Time this query spent with an issuable probe but no free slot.
        double queue_wait_us = 0;
        std::optional<clock_type::time_point> blocked_since;
        uint64_t query_matching = 0;
        uint64_t query_distance = 0;
        uint32_t final_cum_m = 0;
        bool any_missing = false;
        bool decode_error = false;
        bool probe_failed = false;
      };
      // Completions fire on a librados finisher thread: the callback only
      // pushes the slot index and signals. Everything else (get_return_
      // value(), release(), resubmit) happens on this worker thread.
      struct done_queue_t {
        std::mutex lock;
        std::condition_variable cv;
        std::vector<size_t> slots;
        void push(size_t slot) {
          {
            std::lock_guard<std::mutex> locker(lock);
            slots.push_back(slot);
          }
          cv.notify_one();
        }
      };
      done_queue_t done;
      const librados::callback_t probe_complete_cb =
        [](librados::completion_t, void *arg) {
          auto *slot = static_cast<probe_slot_t*>(arg);
          static_cast<done_queue_t*>(slot->queue)->push(slot->self);
        };

      std::vector<std::unique_ptr<query_slot_t>> query_slots;
      query_slots.reserve(query_slot_count);
      for (size_t i = 0; i < query_slot_count; ++i) {
        query_slots.emplace_back(new query_slot_t());
      }
      // Admission order, oldest first: a query that is already in flight
      // always outbids a newer one for a free slot, so head-of-line queries
      // drain instead of every query advancing in lockstep.
      std::vector<size_t> active_order;
      std::vector<std::unique_ptr<probe_slot_t>> slots;
      std::vector<size_t> free_slots;
      size_t outstanding_total = 0;

      // Integral of the outstanding-op count over wall time, so avg_inflight
      // is a time-average rather than a per-completion sample.
      const auto pipeline_begin = clock_type::now();
      auto depth_sampled_at = pipeline_begin;
      double inflight_integral_us = 0;
      size_t max_inflight = 0;
      const auto note_depth = [&]() {
        const auto now = clock_type::now();
        inflight_integral_us +=
          std::chrono::duration<double, std::micro>(now - depth_sampled_at)
            .count() * static_cast<double>(outstanding_total);
        depth_sampled_at = now;
      };

      const auto elapsed_us = [](const query_slot_t& qs,
                                 clock_type::time_point tp) {
        return std::chrono::duration<double, std::micro>(
            tp - qs.query_begin).count();
      };
      const auto build_request = [&](const ceph::bufferlist& query_bl) {
        ceph::rados::query_vectors_request_t req;
        req.bucket_name = options.bucket;
        req.index_name = options.index;
        req.data_type = config.data_type;
        req.distance_metric = config.distance_metric;
        req.dimension = config.dimension;
        req.local_top_k = shortlist_top_k(options);
        req.query_vector = query_bl;
        pg_lsh::apply_boundary_search_config(
            config, query_params(options), &req);
        pg_lsh::apply_tree_boundary_search_config(
            config, query_params(options), &req);
        req.reply_flags =
          (options.reply_include_vector
             ? ceph::rados::query_vectors_reply_include_vector : 0u) |
          (options.reply_omit_tree_stats
             ? ceph::rados::query_vectors_reply_omit_tree_stats : 0u);
        return req;
      };

      // Ground-truth recall of a candidate result set, without touching
      // the run-level counters -- the final recall accounting below still
      // owns those. Only called per stage when a CSV is being written,
      // since it rebuilds the expected-id set each time.
      const auto measure_recall =
        [&](size_t query_index,
            const std::vector<ceph::rados::query_vectors_result_entry_t>&
              results) -> std::optional<double> {
        if (groundtruth == nullptr ||
            query_index >= groundtruth->rows.size()) {
          return std::nullopt;
        }
        const auto& truth = groundtruth->rows[query_index];
        const size_t truth_k = std::min<size_t>(options.top_k, truth.size());
        if (truth_k == 0) {
          return std::nullopt;
        }
        std::set<std::string> expected;
        for (size_t i = 0; i < truth_k; ++i) {
          expected.insert(expected_entry_id(options, truth[i]));
        }
        uint64_t hits = 0;
        for (const auto& entry : results) {
          if (expected.erase(entry.entry_id) != 0) ++hits;
        }
        return static_cast<double>(hits) / static_cast<double>(truth_k);
      };

      // Points a query slot at its next non-empty schedule stage. Stages
      // slice by ranked-PG index, never by flat probe index -- see the
      // pg_probe_ranges comment in admit_query() below.
      const auto begin_stage = [&](query_slot_t& qs) -> bool {
        while (qs.stage < policy.schedule.size()) {
          const auto range =
            sf::stage_range(policy.schedule, qs.stage, qs.available_pgs);
          if (range.empty()) {
            ++qs.stage;
            continue;
          }
          qs.stage_range_begin = range.begin;
          qs.stage_range_end = range.end;
          qs.stage_probe_begin = qs.pg_probe_ranges[range.begin].first;
          qs.probe_end = qs.pg_probe_ranges[range.end - 1].second;
          qs.next_probe = qs.stage_probe_begin;
          // assign() once, never resized while the stage is outstanding:
          // submit_query() holds &oc.reply and &oc.op_rval for the life of
          // the op.
          qs.stage_outcomes.assign(qs.probe_end - qs.stage_probe_begin,
                                   fanout_probe_outcome_t{});
          qs.stage_begin_us = elapsed_us(qs, clock_type::now());
          qs.stage_end_us = 0;
          return true;
        }
        return false;
      };

      bool admitting = true;
      const auto admit_query = [&](query_slot_t& qs, size_t slot_index) -> bool {
        if (stop.load(std::memory_order_relaxed)) return false;
        const uint64_t ticket = next.fetch_add(1, std::memory_order_relaxed);
        if (min_queries == 0 && min_seconds == 0) {
          if (ticket >= fixed_total) return false;
        } else if (ticket >= min_queries) {
          const double elapsed = std::chrono::duration<double>(
              clock_type::now() - begin).count();
          if (elapsed >= min_seconds) return false;
        }
        const size_t query_index = ticket % queries.rows.size();
        // Starts before build_query_probes() so routing/LSH computation is
        // included in logical latency, per spec.
        qs.query_begin = clock_type::now();
        qs.ticket = ticket;
        qs.query_index = query_index;
        qs.query_bl = vector_buffer_typed(queries.rows[query_index],
                                           config.data_type, options.quantize_scale);

        qs.probes.clear();
        uint64_t generated_groups = 0;
        int rr = 0;
        if (options.routing_mode == "pivot") {
          const auto ranked = pivot_rank_pgs(
              queries.rows[query_index].data(), pivot_model->dim,
              *pivot_model, options.pivot_probe_budget);
          std::vector<uint32_t> probe_pgs(
              ranked.begin(),
              ranked.begin() + std::min<size_t>(options.m, ranked.size()));
          generated_groups = probe_pgs.size();
          rr = pivot_query_probes(options, probe_pgs, &cache, &qs.probes);
        } else {
          rr = pg_lsh::build_query_probes(
              options.bucket, options.index,
              vector_buffer(queries.rows[query_index]),
              placement_config(config),
              query_params(options), pool_info(options), &cache, &qs.probes,
              &generated_groups);
          expand_full_pg_scan(options, &qs.probes);
        }
        (void)generated_groups;
        // Partition mode probes every partition of a routed PG, so count
        // routed PGs (partition % pg_num), not partitions.
        std::unordered_set<uint32_t> unique_pgs;
        for (const auto& probe : qs.probes) {
          unique_pgs.insert(routed_pg(options, probe.pg));
        }
        qs.distinct_pgs = unique_pgs.size();
        // Checked on every query, not just the logged sample.
        // probe_limit_per_pg (and distance_bucket_radius/residual_hamming_
        // radius) can make probes.size() exceed options.m -- several sub_oid
        // probes per PG -- so only the PG count is checked, not a 1:1 ratio.
        // --bucket-locators probes up to m distinct current PGs; a query
        // whose LSH groups reach fewer PGs gets fewer probes.
        if (rr < 0 ||
            (!options.bucket_locators && unique_pgs.size() != options.m) ||
            qs.probes.empty() ||
            !validate_oid(options, qs.probes.front().oid.name,
                          qs.probes.front().sub_oid_name)) {
          ++local.failures;
          local.first_error = rr < 0 ? rr : -EINVAL;
          int expected = 0;
          first_error.compare_exchange_strong(expected, local.first_error);
          stop.store(true);
          return false;
        }
        if (local.first_oid.empty()) {
          local.first_oid = qs.probes.front().oid.name;
        }

        // build_query_probes() emits probes PG-major: the outer loop walks
        // the ranked PGs in order and the inner loop emits that PG's
        // sub_oid probes, so one ranked PG's probes are always adjacent.
        // Stages therefore slice by ranked-PG index, never by flat probe
        // index -- the latter would cut a PG in half the moment
        // distance_bucket_radius/residual_hamming_radius puts more than one
        // sub_oid probe on it.
        qs.pg_probe_ranges.clear();
        for (size_t i = 0; i < qs.probes.size(); ++i) {
          if (!qs.pg_probe_ranges.empty() &&
              routed_pg(options, qs.probes[i].pg) ==
                routed_pg(options,
                          qs.probes[qs.pg_probe_ranges.back().first].pg)) {
            qs.pg_probe_ranges.back().second = i + 1;
          } else {
            qs.pg_probe_ranges.emplace_back(i, i + 1);
          }
        }
        qs.available_pgs = static_cast<uint32_t>(qs.pg_probe_ranges.size());

        qs.accumulator.emplace(shortlist_top_k(options));
        qs.outcomes.clear();
        qs.stage_outcomes.clear();
        qs.stage_records.clear();
        qs.tau_prev.reset();
        qs.stage = 0;
        qs.next_probe = 0;
        qs.probe_end = 0;
        qs.outstanding = 0;
        qs.queue_wait_us = 0;
        qs.blocked_since.reset();
        qs.query_matching = 0;
        qs.query_distance = 0;
        qs.final_cum_m = 0;
        qs.any_missing = false;
        qs.decode_error = false;
        qs.probe_failed = false;

        qs.no_stages = !begin_stage(qs);
        if (qs.no_stages) {
          // Empty schedule against the routed PGs: nothing to dispatch.
          qs.next_probe = 0;
          qs.probe_end = 0;
        }
        qs.active = true;
        active_order.push_back(slot_index);
        return true;
      };

      // A free slot, growing the pool when --io-depth is unbounded.
      const auto claim_slot = [&]() -> probe_slot_t* {
        if (free_slots.empty()) {
          if (bounded_depth && slots.size() >= options.io_depth) {
            return nullptr;
          }
          slots.emplace_back(new probe_slot_t());
          slots.back()->queue = &done;
          slots.back()->self = slots.size() - 1;
          free_slots.push_back(slots.size() - 1);
        }
        const size_t index = free_slots.back();
        free_slots.pop_back();
        return slots[index].get();
      };

      // Issues every probe that can be issued right now, oldest query
      // first. Called before any decode/merge work so a slot is never idle
      // while this thread is busy on the CPU.
      const auto submit_ready = [&]() {
        for (const size_t qi : active_order) {
          auto& qs = *query_slots[qi];
          if (!qs.active) continue;
          while (qs.next_probe < qs.probe_end) {
            if (sequential_mode) {
              const size_t pi = qs.next_probe++;
              auto& oc = qs.stage_outcomes[pi - qs.stage_probe_begin];
              oc.pg = qs.probes[pi].pg;
              oc.sub_oid = qs.probes[pi].sub_oid_name;
              oc.ready_us = qs.stage_begin_us;
              oc.submit_us = elapsed_us(qs, clock_type::now());
              ++local.osd_requests;
              ++local.osd_query_ops;
              oc.r = pg_lsh::query_sync(
                  ioctx, qs.probes[pi], build_request(qs.query_bl),
                  &oc.reply, &oc.op_rval);
              oc.complete_us = elapsed_us(qs, clock_type::now());
              if (oc.r < 0 && oc.r != -ENOENT) {
                qs.probe_failed = true;
                // Without this the probe's real errno is lost and
                // complete_query() reports a bare -EIO.
                if (local.first_error == 0) local.first_error = oc.r;
                qs.next_probe = qs.probe_end;
              }
              continue;
            }
            probe_slot_t *slot = claim_slot();
            if (slot == nullptr) break;
            const size_t pi = qs.next_probe++;
            slot->query_slot = qi;
            slot->probe_index = pi;
            slot->outcome_index = pi - qs.stage_probe_begin;
            slot->op_state.reset(new pg_lsh::query_op_state_t());
            slot->completion = librados::Rados::aio_create_completion(
                slot, probe_complete_cb);
            auto& oc = qs.stage_outcomes[slot->outcome_index];
            oc.pg = qs.probes[pi].pg;
            oc.sub_oid = qs.probes[pi].sub_oid_name;
            oc.ready_us = qs.stage_begin_us;
            oc.submit_us = elapsed_us(qs, clock_type::now());
            note_depth();
            ++local.osd_requests;
            ++local.osd_query_ops;
            ++outstanding_total;
            ++qs.outstanding;
            if (outstanding_total > max_inflight) {
              max_inflight = outstanding_total;
            }
            oc.r = pg_lsh::submit_query(
                ioctx, qs.probes[pi], build_request(qs.query_bl),
                slot->op_state.get(), slot->completion, &oc.reply,
                &oc.op_rval);
            if (oc.r != 0) {
              // Never reached the wire, so no callback will ever fire for
              // this slot: retire it here.
              note_depth();
              --outstanding_total;
              --qs.outstanding;
              oc.complete_us = elapsed_us(qs, clock_type::now());
              slot->completion->release();
              slot->completion = nullptr;
              free_slots.push_back(slot->self);
              if (oc.r != -ENOENT) {
                qs.probe_failed = true;
                if (local.first_error == 0) local.first_error = oc.r;
              }
            }
          }
          if (qs.stage_end_us == 0 && qs.outstanding == 0 &&
              qs.next_probe >= qs.probe_end) {
            qs.stage_end_us = elapsed_us(qs, clock_type::now());
          }
          // Queue wait is only the time this query was starved for a slot
          // -- not the time its own ops were on the wire.
          const bool blocked = qs.next_probe < qs.probe_end;
          const auto now = clock_type::now();
          if (blocked && !qs.blocked_since) {
            qs.blocked_since = now;
          } else if (!blocked && qs.blocked_since) {
            qs.queue_wait_us += std::chrono::duration<double, std::micro>(
                now - *qs.blocked_since).count();
            qs.blocked_since.reset();
          }
        }
      };

      const auto reap_done = [&]() {
        std::vector<size_t> reaped;
        {
          std::unique_lock<std::mutex> locker(done.lock);
          done.cv.wait(locker, [&] { return !done.slots.empty(); });
          reaped.swap(done.slots);
        }
        for (const size_t slot_index : reaped) {
          auto& slot = *slots[slot_index];
          auto& qs = *query_slots[slot.query_slot];
          auto& oc = qs.stage_outcomes[slot.outcome_index];
          oc.r = slot.completion->get_return_value();
          oc.complete_us = elapsed_us(qs, clock_type::now());
          slot.completion->release();
          slot.completion = nullptr;
          note_depth();
          --outstanding_total;
          --qs.outstanding;
          if (qs.outstanding == 0 && qs.next_probe >= qs.probe_end) {
            qs.stage_end_us = oc.complete_us;
          }
          if (oc.r < 0 && oc.r != -ENOENT) {
            qs.probe_failed = true;
            if (local.first_error == 0) local.first_error = oc.r;
          }
          free_slots.push_back(slot_index);
        }
      };

      // Everything that happens once a stage's probes have all landed:
      // decode, per-probe stats, global top-k merge, stage record, and the
      // selective-fan-out stop decision. Returns true when the logical
      // query is finished (schedule exhausted, stop rule fired, or the
      // stage failed). Stage decisions stay strictly sequential WITHIN a
      // query -- only distinct logical queries ever overlap -- so the
      // candidate set is identical to the pre-pipeline dispatcher's.
      const auto finish_stage = [&](query_slot_t& qs) -> bool {
        auto& accumulator = *qs.accumulator;
        auto& stage_outcomes = qs.stage_outcomes;
        auto& outcomes = qs.outcomes;
        auto& stage_records = qs.stage_records;
        auto& tau_prev = qs.tau_prev;
        auto& any_missing = qs.any_missing;
        auto& decode_error = qs.decode_error;
        auto& query_matching = qs.query_matching;
        auto& query_distance = qs.query_distance;
        auto& final_cum_m = qs.final_cum_m;
        const size_t stage = qs.stage;
        const sf::stage_range_t range{qs.stage_range_begin, qs.stage_range_end};
        const double stage_begin_us = qs.stage_begin_us;
        const double stage_end_us = qs.stage_end_us;
        if (qs.no_stages) {
          return true;
        }
        if (qs.probe_failed) {
          return true;
        }
        std::vector<ceph::rados::query_vectors_result_entry_t> stage_entries;
        uint64_t stage_matching = 0;
        uint64_t stage_distance = 0;
        for (auto& oc : stage_outcomes) {
          if (oc.r == -ENOENT || oc.op_rval == -ENOENT) {
            any_missing = true;
            continue;
          }
          if (oc.reply.length() == 0) {
            decode_error = true;
            // An empty reply on a probe the OSD did not answer with
            // -ENOENT: keep whatever status it did send, so the run
            // reports that instead of a bare -EIO.
            if (local.first_error == 0) {
              local.first_error = oc.op_rval != 0 ? oc.op_rval : oc.r;
            }
            break;
          }
          ceph::rados::query_vectors_result_t partial;
          try {
            auto p = oc.reply.cbegin();
            decode(partial, p);
            if (!p.end()) throw ceph::buffer::malformed_input("trailing reply");
          } catch (const ceph::buffer::error&) { decode_error = true; break; }
          local.reply_bytes += oc.reply.length();
          for (const auto& e : partial.entries) {
            local.reply_vector_bytes += e.vector.length();
          }
          oc.matching_records = partial.local_matching_entries;
          oc.distance_computations = partial.local_distance_computations;
          stage_matching += partial.local_matching_entries;
          stage_distance += partial.local_distance_computations;
          if (partial.tree_stats) {
            const auto& ts = *partial.tree_stats;
            ++local.tree_stats_probes;
            local.tree_primary_candidate_count += ts.primary_candidate_count;
            local.tree_expanded_subtree_count += ts.expanded_subtree_count;
            local.tree_pruned_distance_count += ts.pruned_distance_count;
            local.tree_pruned_residual_count += ts.pruned_residual_count;
            local.tree_opened_onode_count += ts.opened_onode_count;
            local.tree_candidates_seen += ts.candidates_seen;
            local.tree_containment_violations += ts.containment_violations;
          }
          for (auto& entry : partial.entries) {
            stage_entries.push_back(std::move(entry));
          }
        }
        if (decode_error) {
          return true;
        }

        // Global merge dedups by entry_id, not entry.key. entry.key is
        // restored server-side from OMAP per entry_id (pg_backend.cc's
        // query_vectors handler) and that restoration is currently
        // unreliable (separate, undiagnosed issue -- see
        // expected_entry_id()'s comment above); entry_id itself is
        // returned verbatim from the LIST scan and does not depend on it.
        const uint64_t dupes_before = accumulator.dropped_duplicates();
        const auto merge_begin = clock_type::now();
        const uint32_t replacements = accumulator.merge_stage(stage_entries);
        local.duplicate_entries +=
          accumulator.dropped_duplicates() - dupes_before;

        query_matching += stage_matching;
        query_distance += stage_distance;
        final_cum_m = range.end;

        sf::stage_record_t record;
        record.stage = stage;
        record.cum_m = range.end;
        record.batch_pg_count = range.count();
        record.d1 = accumulator.current_d1();
        record.tau = accumulator.current_tau();
        record.topk_replacements = replacements;
        record.topk_size =
          static_cast<uint32_t>(accumulator.results().size());
        record.batch_candidates = stage_matching;
        record.cum_candidates = query_matching;
        record.batch_distance_computations = stage_distance;
        record.cum_distance_computations = query_distance;
        record.batch_latency_us = stage_end_us - stage_begin_us;
        record.cum_latency_us = stage_end_us;
        if (selective_csv != nullptr) {
          record.recall =
            measure_recall(qs.query_index, accumulator.results());
        }
        sf::finalize_stage_record(tau_prev, &record);
        tau_prev = record.tau;

        outcomes.insert(outcomes.end(),
                        std::make_move_iterator(stage_outcomes.begin()),
                        std::make_move_iterator(stage_outcomes.end()));
        stage_records.push_back(std::move(record));

        if (!sf::should_expand(policy, stage_records.back())) {
          return true;
        }
        ++qs.stage;
        return !begin_stage(qs);
      };

      // Retires a finished logical query. The end-to-end latency keeps its
      // old meaning exactly -- query_begin to query_end, slot queueing
      // included -- and the queue/active split is reported alongside it so
      // a depth sweep stays comparable with pre-pipeline runs.
      const auto complete_query = [&](query_slot_t& qs) {
        auto& accumulator = *qs.accumulator;
        const auto& probes = qs.probes;
        const auto& outcomes = qs.outcomes;
        const auto& stage_records = qs.stage_records;
        const size_t query_index = qs.query_index;
        const uint64_t ticket = qs.ticket;
        const auto& query_bl = qs.query_bl;
        const uint32_t final_cum_m = qs.final_cum_m;
        if (qs.blocked_since) {
          qs.queue_wait_us += std::chrono::duration<double, std::micro>(
              clock_type::now() - *qs.blocked_since).count();
          qs.blocked_since.reset();
        }
        if (qs.probe_failed || qs.decode_error) {
          ++local.failures;
          if (local.first_error == 0) local.first_error = -EIO;
          int expected = 0;
          first_error.compare_exchange_strong(expected, local.first_error);
          stop.store(true);
          return;
        }

        const auto& merged = accumulator.results();
        if (qs.any_missing && merged.empty()) ++local.missing_objects;
        local.returned_entries += merged.size();
        local.probe_matching_records += qs.query_matching;
        local.probe_distance_computations += qs.query_distance;
        local.selective_final_m_sum += final_cum_m;
        local.selective_stages_sum += stage_records.size();
        ++local.selective_final_m_histogram[final_cum_m];
        if (groundtruth != nullptr && query_index < groundtruth->rows.size()) {
          const auto& truth = groundtruth->rows[query_index];
          const size_t truth_k = std::min<size_t>(options.top_k, truth.size());
          std::set<std::string> expected;
          for (size_t i = 0; i < truth_k; ++i) {
            expected.insert(expected_entry_id(options, truth[i]));
          }
          if (options.rerank != nullptr) {
            // Rescore the shortlist against the ORIGINAL float32 query and
            // vectors, then score recall on the reranked top_k. Only the
            // ordering changes; which vectors were reachable was already
            // fixed by routing.
            for (const auto *entry_id : rerank_shortlist(
                     *options.rerank, queries.rows[query_index], merged,
                     options.top_k, &local.rerank_unmapped)) {
              if (expected.erase(*entry_id) != 0) ++local.recall_hits;
            }
          } else {
            for (const auto& entry : merged) {
              if (expected.erase(entry.entry_id) != 0) ++local.recall_hits;
            }
          }
          local.recall_denominator += truth_k;
        }

        const auto query_end = clock_type::now();
        const double end_to_end_us = std::chrono::duration<double, std::micro>(
            query_end - qs.query_begin).count();
        ++local.completed;
        // queue_wait: time this query had an issuable probe but no free
        // slot. active = end-to-end minus that -- the span where its ops
        // were actually on the wire, server-side queueing included.
        const double queue_wait_us = std::min(qs.queue_wait_us, end_to_end_us);
        local.queue_wait_us_sum += static_cast<uint64_t>(queue_wait_us);
        local.active_us_sum +=
            static_cast<uint64_t>(end_to_end_us - queue_wait_us);
        if (measure) {
          local.latency_us.push_back(end_to_end_us);
        }
        if (selective_csv != nullptr) {
          std::ostringstream rows;
          for (const auto& record : stage_records) {
            sf::write_stage_csv_row(rows, query_index, record);
          }
          std::lock_guard<std::mutex> locker(log_lock);
          *selective_csv << rows.str();
        }
        if (results_csv != nullptr) {
          write_query_results_csv(results_csv, log_lock, query_index, merged);
        }

        if (fanout_log != nullptr && ticket < options.fanout_log_count) {
          std::ostringstream block;
          block << "logical_query_id=" << ticket << '\n'
                << "query_index=" << query_index << '\n'
                << "query_vector_hash="
                << librados::vector_placement::hash_v0_vector_hash(query_bl) << '\n'
                << "mode=" << options.fanout_mode << '\n'
                << "target_pg_count=" << probes.size() << '\n'
                << "target_pgs=[";
          for (size_t i = 0; i < probes.size(); ++i) {
            if (i != 0) block << ',';
            block << probes[i].pg;
          }
          block << "]\n"
                << "runtime_distinct_pgs=" << qs.distinct_pgs << '\n'
                << "stages_run=" << stage_records.size() << '\n'
                << "final_cum_m=" << final_cum_m << '\n';
          for (const auto& record : stage_records) {
            block << "stage " << record.stage
                  << " -> cum_m=" << record.cum_m
                  << " batch_pgs=" << record.batch_pg_count
                  << " tau=";
            if (record.tau) block << *record.tau; else block << "unfilled";
            block << " topk_replacements=" << record.topk_replacements
                  << " batch_candidates=" << record.batch_candidates
                  << " batch_latency_us=" << record.batch_latency_us << '\n';
          }
          for (size_t i = 0; i < outcomes.size(); ++i) {
            block << "subrequest " << i << " -> pg=" << outcomes[i].pg
                  << " sub_oid=" << outcomes[i].sub_oid
                  << " min_hamming_distance=" << probes[i].min_hamming_distance
                  // table_vote_count is currently always 1 (pg_lsh_v0_select_
                  // unique_pgs() never aggregates votes across tables despite
                  // the field name) -- logged for reference only, not a real
                  // confidence signal.
                  << " table_vote_count_ref_only=" << probes[i].table_vote_count
                  << " matching_records=" << outcomes[i].matching_records
                  << " distance_computations=" << outcomes[i].distance_computations
                  << " r=" << outcomes[i].r
                  << " op_rval=" << outcomes[i].op_rval
                  << " ready_us=" << outcomes[i].ready_us
                  << " submit_us=" << outcomes[i].submit_us
                  << " complete_us=" << outcomes[i].complete_us;
            block << '\n';
          }
          std::lock_guard<std::mutex> locker(log_lock);
          *fanout_log << block.str() << "---\n";
          fanout_log->flush();
        }
      };

      // Finishes every query whose current stage has fully landed. Runs
      // after submit_ready(), never before it.
      const auto finish_ready_stages = [&]() {
        for (size_t idx = 0; idx < active_order.size(); ) {
          auto& qs = *query_slots[active_order[idx]];
          if (qs.outstanding != 0 || qs.next_probe < qs.probe_end) {
            ++idx;
            continue;
          }
          if (finish_stage(qs)) {
            complete_query(qs);
            qs.active = false;
            qs.probes.clear();
            qs.outcomes.clear();
            qs.stage_outcomes.clear();
            qs.accumulator.reset();
            active_order.erase(active_order.begin() + idx);
          } else {
            ++idx;
          }
        }
      };

      for (;;) {
        submit_ready();
        while (admitting) {
          size_t idle_slot = query_slots.size();
          for (size_t i = 0; i < query_slots.size(); ++i) {
            if (!query_slots[i]->active) { idle_slot = i; break; }
          }
          if (idle_slot == query_slots.size()) break;
          // Only admit when a probe slot is actually available: admitting
          // into a saturated pipeline would grow the backlog (and the
          // reported end-to-end latency) without raising queue depth.
          if (!sequential_mode && free_slots.empty() && bounded_depth &&
              slots.size() >= options.io_depth) {
            break;
          }
          if (!admit_query(*query_slots[idle_slot], idle_slot)) {
            admitting = false;
            break;
          }
          submit_ready();
        }
        if (active_order.empty() && outstanding_total == 0) {
          break;
        }
        if (outstanding_total != 0) {
          reap_done();
          // Refill before any decode/merge work so no slot idles while
          // this thread is on the CPU -- this ordering is what actually
          // holds the queue depth.
          submit_ready();
        }
        finish_ready_stages();
      }
      // Callbacks reference worker-local slots, so nothing may go out of
      // scope while an op is still outstanding. The loop above only exits
      // at zero, but a future early return must drain here too.
      while (outstanding_total != 0) {
        reap_done();
      }
      note_depth();
      local.inflight_integral_us += inflight_integral_us;
      local.pipeline_wall_us += std::chrono::duration<double, std::micro>(
          clock_type::now() - pipeline_begin).count();
      if (max_inflight > local.max_inflight) local.max_inflight = max_inflight;
      if (run_hook) run_measure_hook(options.measure_hook, "end");
    });
  }
  for (auto& worker : workers) worker.join();
  result.seconds = std::chrono::duration<double>(clock_type::now() - begin).count();
  result.first_error = first_error.load();
  for (auto& worker : worker_results) {
    auto& local = worker.result;
    result.completed += local.completed;
    result.failures += local.failures;
    result.missing_objects += local.missing_objects;
    result.duplicate_entries += local.duplicate_entries;
    result.returned_entries += local.returned_entries;
    result.probe_matching_records += local.probe_matching_records;
    result.probe_distance_computations += local.probe_distance_computations;
    result.recall_hits += local.recall_hits;
    result.recall_denominator += local.recall_denominator;
    result.rerank_unmapped += local.rerank_unmapped;
    result.latency_us.insert(result.latency_us.end(),
                             local.latency_us.begin(), local.latency_us.end());
    if (result.first_oid.empty() && !local.first_oid.empty()) {
      result.first_oid = local.first_oid;
    }
    result.reply_bytes += local.reply_bytes;
    result.reply_vector_bytes += local.reply_vector_bytes;
    result.tree_stats_probes += local.tree_stats_probes;
    result.tree_primary_candidate_count += local.tree_primary_candidate_count;
    result.tree_expanded_subtree_count += local.tree_expanded_subtree_count;
    result.tree_pruned_distance_count += local.tree_pruned_distance_count;
    result.tree_pruned_residual_count += local.tree_pruned_residual_count;
    result.tree_opened_onode_count += local.tree_opened_onode_count;
    result.tree_candidates_seen += local.tree_candidates_seen;
    result.tree_containment_violations += local.tree_containment_violations;
    result.selective_final_m_sum += local.selective_final_m_sum;
    result.selective_stages_sum += local.selective_stages_sum;
    for (const auto& [final_m, count] : local.selective_final_m_histogram) {
      result.selective_final_m_histogram[final_m] += count;
    }
    result.queue_wait_us_sum += local.queue_wait_us_sum;
    result.active_us_sum += local.active_us_sum;
    result.inflight_integral_us += local.inflight_integral_us;
    result.pipeline_wall_us += local.pipeline_wall_us;
    result.max_inflight = std::max(result.max_inflight, local.max_inflight);
    result.osd_requests += local.osd_requests;
    result.osd_query_ops += local.osd_query_ops;
  }
  return result;
}

// --query-batch-size dispatcher.
//
// Everything one logical query does is unchanged: the same
// build_query_probes()/pivot_query_probes() routing, the same m probes with
// the same request payloads, and the same entry_id-dedup global top-k merge.
// The only difference is packing. A worker resolves query_batch_size logical
// queries up front, then every probe among them that resolved to the SAME
// object shares one RADOS request -- one ObjectOperation holding one
// QUERY_VECTORS op per probe. Since the ops are byte-identical to what the
// one-probe-per-request dispatcher sends, the OSD and SeaStore paths need no
// batch awareness at all; only the number of MOSDOp/MOSDOpReply pairs drops.
//
// Grouping is by object, not by PG, because a RADOS op vector addresses
// exactly one object. Under --routing-mode pivot/pg-container that is the
// same thing (one object per PG), which is the case this exists for; under
// pg-suboid each query has its own anchor sub_oid object and probes only
// share a request when those collide.
//
// A batch is submitted in full and then waited on in full -- the same
// submit-all/wait-all shape run_fanout_queries() has at --io-depth 0, which
// is why --io-depth and --selective are rejected alongside this option.
fanout_query_result_t run_batch_queries(
    const options_t& options,
    const float_dataset_t& queries,
    const ivec_dataset_t *groundtruth,
    const pg_lsh::index_config_t& config,
    librados::Rados& cluster,
    std::shared_ptr<pg_lsh::locator_state_t> locator_state,
    uint32_t rounds,
    uint64_t min_queries,
    uint32_t min_seconds,
    bool measure,
    const PivotModel *pivot_model = nullptr,
    std::ofstream *results_csv = nullptr)
{
  fanout_query_result_t result;
  std::mutex log_lock;
  const uint64_t fixed_total =
    static_cast<uint64_t>(queries.rows.size()) * rounds;
  std::atomic<uint64_t> next{0};
  std::atomic<bool> stop{false};
  std::atomic<int> first_error{0};
  const auto begin = clock_type::now();
  const size_t batch_size = options.query_batch_size;

  struct worker_result_t { fanout_query_result_t result; };
  std::vector<worker_result_t> worker_results(options.query_concurrency);
  std::vector<std::thread> workers;
  workers.reserve(options.query_concurrency);

  for (uint32_t worker_id = 0; worker_id < options.query_concurrency; ++worker_id) {
    workers.emplace_back([&, worker_id] {
      auto& local = worker_results[worker_id].result;
      librados::IoCtx ioctx;
      int r = cluster.ioctx_create(options.pool.c_str(), ioctx);
      if (r < 0) {
        local.failures = 1;
        local.first_error = r;
        int expected = 0;
        first_error.compare_exchange_strong(expected, r);
        stop.store(true);
        return;
      }
      pg_lsh::locator_cache_t cache(
          &ioctx, pool_info(options), options.pool, options.bucket,
          options.index, locator_state, options.partition_num,
      options.bucket_locators, options.k);
      r = cache.precompute_all();
      if (r < 0) {
        local.failures = 1;
        local.first_error = r;
        int expected = 0;
        first_error.compare_exchange_strong(expected, r);
        stop.store(true);
        return;
      }

      // One logical query of a batch. Identical bookkeeping to
      // run_fanout_queries()'s query_slot_t, minus the staging fields --
      // batching runs the whole routed probe set in one shot.
      struct batch_query_t {
        uint64_t ticket = 0;
        size_t query_index = 0;
        clock_type::time_point query_begin{};
        ceph::bufferlist query_bl;
        std::vector<pg_lsh::query_probe_t> probes;
        std::optional<sf::topk_accumulator_t<
            ceph::rados::query_vectors_result_entry_t>> accumulator;
        // Replies buffered by probe index, not merged as they land. The
        // non-batch dispatcher merges a stage once, walking its probes in
        // index order; group completion order here is arbitrary, and
        // merge_stage() sorts on distance alone with an unstable sort, so
        // merging on arrival would let two entries at the SAME distance
        // come out in a different order (and, at the top-k cutoff, let a
        // different one of the two survive). Buffering restores the
        // existing path's exact merge order.
        std::vector<std::vector<
            ceph::rados::query_vectors_result_entry_t>> probe_entries;
        uint64_t query_matching = 0;
        uint64_t query_distance = 0;
        uint32_t distinct_pgs = 0;
        size_t outstanding_groups = 0;
        bool any_missing = false;
        bool decode_error = false;
        bool probe_failed = false;
        double complete_us = 0;
      };
      // The probes of one object, across every query in the batch. Members
      // are (query slot, probe index) so a reply can be routed back to the
      // query that asked for it -- the "original query id" the merge needs.
      struct probe_group_t {
        pg_lsh::query_probe_t probe;
        std::vector<std::pair<size_t, size_t>> members;
        // Sized once, before any pointer into it is taken: submit_query_multi
        // holds &replies[i]/&rvals[i] for the life of the op.
        std::vector<ceph::bufferlist> replies;
        std::vector<int> rvals;
        std::unique_ptr<pg_lsh::query_op_state_t> op_state;
        librados::AioCompletion *completion = nullptr;
        void *queue = nullptr;
        size_t self = 0;
        bool inflight = false;
        int r = 0;
      };
      // Same split as run_fanout_queries(): the librados finisher thread
      // only records which group landed; everything else runs here.
      struct done_queue_t {
        std::mutex lock;
        std::condition_variable cv;
        std::vector<size_t> groups;
        void push(size_t group) {
          {
            std::lock_guard<std::mutex> locker(lock);
            groups.push_back(group);
          }
          cv.notify_one();
        }
      };
      done_queue_t done;
      const librados::callback_t group_complete_cb =
        [](librados::completion_t, void *arg) {
          auto *group = static_cast<probe_group_t*>(arg);
          static_cast<done_queue_t*>(group->queue)->push(group->self);
        };

      const auto build_request = [&](const ceph::bufferlist& query_bl) {
        ceph::rados::query_vectors_request_t req;
        req.bucket_name = options.bucket;
        req.index_name = options.index;
        req.data_type = config.data_type;
        req.distance_metric = config.distance_metric;
        req.dimension = config.dimension;
        req.local_top_k = shortlist_top_k(options);
        req.query_vector = query_bl;
        pg_lsh::apply_boundary_search_config(
            config, query_params(options), &req);
        pg_lsh::apply_tree_boundary_search_config(
            config, query_params(options), &req);
        req.reply_flags =
          (options.reply_include_vector
             ? ceph::rados::query_vectors_reply_include_vector : 0u) |
          (options.reply_omit_tree_stats
             ? ceph::rados::query_vectors_reply_omit_tree_stats : 0u);
        return req;
      };

      // Same admission rule as run_fanout_queries()::admit_query().
      const auto claim_ticket = [&](uint64_t *ticket) -> bool {
        if (stop.load(std::memory_order_relaxed)) return false;
        const uint64_t claimed = next.fetch_add(1, std::memory_order_relaxed);
        if (min_queries == 0 && min_seconds == 0) {
          if (claimed >= fixed_total) return false;
        } else if (claimed >= min_queries) {
          const double elapsed = std::chrono::duration<double>(
              clock_type::now() - begin).count();
          if (elapsed >= min_seconds) return false;
        }
        *ticket = claimed;
        return true;
      };

      // Routing for one logical query. Byte-for-byte the same calls
      // run_fanout_queries() makes, including the same validation.
      const auto route_query = [&](batch_query_t& bq) -> bool {
        bq.query_begin = clock_type::now();
        bq.query_index = bq.ticket % queries.rows.size();
        bq.query_bl = vector_buffer_typed(queries.rows[bq.query_index],
                                       config.data_type, options.quantize_scale);
        bq.probes.clear();
        uint64_t generated_groups = 0;
        int rr = 0;
        if (options.routing_mode == "pivot") {
          const auto ranked = pivot_rank_pgs(
              queries.rows[bq.query_index].data(), pivot_model->dim,
              *pivot_model, options.pivot_probe_budget);
          std::vector<uint32_t> probe_pgs(
              ranked.begin(),
              ranked.begin() + std::min<size_t>(options.m, ranked.size()));
          generated_groups = probe_pgs.size();
          rr = pivot_query_probes(options, probe_pgs, &cache, &bq.probes);
        } else {
          rr = pg_lsh::build_query_probes(
              options.bucket, options.index,
              vector_buffer(queries.rows[bq.query_index]),
              placement_config(config),
              query_params(options), pool_info(options), &cache, &bq.probes,
              &generated_groups);
          expand_full_pg_scan(options, &bq.probes);
        }
        (void)generated_groups;
        std::unordered_set<uint32_t> unique_pgs;
        for (const auto& probe : bq.probes) {
          unique_pgs.insert(routed_pg(options, probe.pg));
        }
        bq.distinct_pgs = static_cast<uint32_t>(unique_pgs.size());
        if (rr < 0 ||
            (!options.bucket_locators && unique_pgs.size() != options.m) ||
            bq.probes.empty() ||
            !validate_oid(options, bq.probes.front().oid.name,
                          bq.probes.front().sub_oid_name)) {
          ++local.failures;
          local.first_error = rr < 0 ? rr : -EINVAL;
          int expected = 0;
          first_error.compare_exchange_strong(expected, local.first_error);
          stop.store(true);
          return false;
        }
        if (local.first_oid.empty()) {
          local.first_oid = bq.probes.front().oid.name;
        }
        bq.accumulator.emplace(shortlist_top_k(options));
        bq.probe_entries.assign(bq.probes.size(), {});
        return true;
      };

      // Records one op's reply against the query that asked for it. The
      // entries are only stashed here, indexed by probe; see
      // batch_query_t::probe_entries for why they are not merged yet.
      const auto consume_reply = [&](batch_query_t& bq,
                                     probe_group_t& group, size_t j) {
        if (group.r == -ENOENT || group.rvals[j] == -ENOENT) {
          bq.any_missing = true;
          return;
        }
        if (group.r < 0) {
          bq.probe_failed = true;
          if (local.first_error == 0) local.first_error = group.r;
          return;
        }
        if (group.replies[j].length() == 0) {
          bq.decode_error = true;
          if (local.first_error == 0) {
            local.first_error =
              group.rvals[j] != 0 ? group.rvals[j] : group.r;
          }
          return;
        }
        ceph::rados::query_vectors_result_t partial;
        try {
          auto p = group.replies[j].cbegin();
          decode(partial, p);
          if (!p.end()) {
            throw ceph::buffer::malformed_input("trailing reply");
          }
        } catch (const ceph::buffer::error&) {
          bq.decode_error = true;
          return;
        }
        bq.query_matching += partial.local_matching_entries;
        bq.query_distance += partial.local_distance_computations;
        if (partial.tree_stats) {
          const auto& ts = *partial.tree_stats;
          ++local.tree_stats_probes;
          local.tree_primary_candidate_count += ts.primary_candidate_count;
          local.tree_expanded_subtree_count += ts.expanded_subtree_count;
          local.tree_pruned_distance_count += ts.pruned_distance_count;
          local.tree_pruned_residual_count += ts.pruned_residual_count;
          local.tree_opened_onode_count += ts.opened_onode_count;
          local.tree_candidates_seen += ts.candidates_seen;
          local.tree_containment_violations += ts.containment_violations;
        }
        bq.probe_entries[group.members[j].second] =
          std::move(partial.entries);
      };

      // One merge per logical query, over its probes in index order --
      // byte-for-byte the order run_fanout_queries()'s single per-stage
      // merge uses, so group completion order cannot affect the top-k.
      const auto merge_query = [&](batch_query_t& bq) {
        std::vector<ceph::rados::query_vectors_result_entry_t> entries;
        for (auto& probe : bq.probe_entries) {
          for (auto& entry : probe) {
            entries.push_back(std::move(entry));
          }
        }
        const uint64_t dupes_before = bq.accumulator->dropped_duplicates();
        bq.accumulator->merge_stage(std::move(entries));
        local.duplicate_entries +=
          bq.accumulator->dropped_duplicates() - dupes_before;
      };

      // Retires one logical query. Deliberately the same accounting as
      // run_fanout_queries()::complete_query(), so a batch run and a
      // non-batch run report the same quantities.
      const auto complete_query = [&](batch_query_t& bq) {
        if (bq.probe_failed || bq.decode_error) {
          ++local.failures;
          if (local.first_error == 0) local.first_error = -EIO;
          int expected = 0;
          first_error.compare_exchange_strong(expected, local.first_error);
          stop.store(true);
          return;
        }
        if (bq.complete_us == 0) {
          // Every group failed to reach the wire, so no reap ever stamped
          // this query. Still report a real end-to-end number.
          bq.complete_us = std::chrono::duration<double, std::micro>(
              clock_type::now() - bq.query_begin).count();
        }
        const auto& merged = bq.accumulator->results();
        if (bq.any_missing && merged.empty()) ++local.missing_objects;
        local.returned_entries += merged.size();
        local.probe_matching_records += bq.query_matching;
        local.probe_distance_computations += bq.query_distance;
        local.selective_final_m_sum += options.m;
        local.selective_stages_sum += 1;
        ++local.selective_final_m_histogram[options.m];
        if (groundtruth != nullptr &&
            bq.query_index < groundtruth->rows.size()) {
          const auto& truth = groundtruth->rows[bq.query_index];
          const size_t truth_k = std::min<size_t>(options.top_k, truth.size());
          std::set<std::string> expected;
          for (size_t i = 0; i < truth_k; ++i) {
            expected.insert(expected_entry_id(options, truth[i]));
          }
          if (options.rerank != nullptr) {
            // Rescore the shortlist against the ORIGINAL float32 query and
            // vectors, then score recall on the reranked top_k. Only the
            // ordering changes; which vectors were reachable was already
            // fixed by routing.
            for (const auto *entry_id : rerank_shortlist(
                     *options.rerank, queries.rows[bq.query_index], merged,
                     options.top_k, &local.rerank_unmapped)) {
              if (expected.erase(*entry_id) != 0) ++local.recall_hits;
            }
          } else {
            for (const auto& entry : merged) {
              if (expected.erase(entry.entry_id) != 0) ++local.recall_hits;
            }
          }
          local.recall_denominator += truth_k;
        }
        ++local.completed;
        if (measure) {
          local.latency_us.push_back(bq.complete_us);
        }
        if (results_csv != nullptr) {
          write_query_results_csv(results_csv, log_lock, bq.query_index, merged);
        }
      };

      std::vector<batch_query_t> batch;
      batch.reserve(batch_size);
      for (;;) {
        batch.clear();
        while (batch.size() < batch_size) {
          uint64_t ticket = 0;
          if (!claim_ticket(&ticket)) break;
          batch.emplace_back();
          batch.back().ticket = ticket;
          if (!route_query(batch.back())) {
            batch.pop_back();
            break;
          }
        }
        if (batch.empty()) break;

        // Regroup: probes of different queries that resolved to the same
        // object become one request. Insertion order is preserved so a
        // batch of 1 issues exactly the probe order a non-batch run does.
        std::vector<probe_group_t> groups;
        std::unordered_map<std::string, size_t> group_of_oid;
        for (size_t qi = 0; qi < batch.size(); ++qi) {
          for (size_t pi = 0; pi < batch[qi].probes.size(); ++pi) {
            const auto& probe = batch[qi].probes[pi];
            // An object is (name, locator): the name alone does not say
            // where RADOS stores it.
            auto [it, inserted] = group_of_oid.emplace(
              probe.oid.name + '\0' + probe.locator_key, groups.size());
            if (inserted) {
              groups.emplace_back();
              groups.back().probe = probe;
            }
            groups[it->second].members.emplace_back(qi, pi);
          }
        }

        for (size_t gi = 0; gi < groups.size(); ++gi) {
          auto& group = groups[gi];
          group.replies.resize(group.members.size());
          group.rvals.assign(group.members.size(), 0);
          group.queue = &done;
          group.self = gi;
          std::vector<ceph::rados::query_vectors_request_t> reqs;
          std::vector<ceph::bufferlist*> reply_ptrs;
          std::vector<int*> rval_ptrs;
          reqs.reserve(group.members.size());
          reply_ptrs.reserve(group.members.size());
          rval_ptrs.reserve(group.members.size());
          for (size_t j = 0; j < group.members.size(); ++j) {
            reqs.push_back(build_request(batch[group.members[j].first].query_bl));
            reply_ptrs.push_back(&group.replies[j]);
            rval_ptrs.push_back(&group.rvals[j]);
          }
          group.op_state.reset(new pg_lsh::query_op_state_t());
          group.completion = librados::Rados::aio_create_completion(
              &group, group_complete_cb);
          for (const auto& member : group.members) {
            ++batch[member.first].outstanding_groups;
          }
          ++local.osd_requests;
          local.osd_query_ops += group.members.size();
          group.r = pg_lsh::submit_query_multi(
              ioctx, group.probe, reqs, group.op_state.get(),
              group.completion, reply_ptrs, rval_ptrs);
          if (group.r == 0) {
            group.inflight = true;
          } else {
            // Never reached the wire, so no callback will fire for it.
            group.completion->release();
            group.completion = nullptr;
            for (const auto& member : group.members) {
              --batch[member.first].outstanding_groups;
              if (group.r != -ENOENT) {
                batch[member.first].probe_failed = true;
                if (local.first_error == 0) local.first_error = group.r;
              } else {
                batch[member.first].any_missing = true;
              }
            }
          }
        }

        // Wait for every group of this batch, then merge. Groups are
        // reaped as they land; a query is merged only when all of its
        // groups have, which keeps the merged candidate set identical to
        // the non-batch dispatcher's.
        size_t outstanding = 0;
        for (const auto& group : groups) {
          if (group.inflight) ++outstanding;
        }
        while (outstanding != 0) {
          std::vector<size_t> reaped;
          {
            std::unique_lock<std::mutex> locker(done.lock);
            done.cv.wait(locker, [&] { return !done.groups.empty(); });
            reaped.swap(done.groups);
          }
          const auto reaped_at = clock_type::now();
          for (const size_t gi : reaped) {
            auto& group = groups[gi];
            group.r = group.completion->get_return_value();
            group.completion->release();
            group.completion = nullptr;
            group.inflight = false;
            --outstanding;
            for (size_t j = 0; j < group.members.size(); ++j) {
              auto& bq = batch[group.members[j].first];
              const double elapsed_us =
                std::chrono::duration<double, std::micro>(
                    reaped_at - bq.query_begin).count();
              if (elapsed_us > bq.complete_us) bq.complete_us = elapsed_us;
              --bq.outstanding_groups;
              consume_reply(bq, group, j);
              if (bq.outstanding_groups == 0) {
                merge_query(bq);
              }
            }
          }
        }

        for (auto& bq : batch) {
          complete_query(bq);
        }
        if (stop.load(std::memory_order_relaxed)) break;
      }
    });
  }
  for (auto& worker : workers) worker.join();
  result.seconds = std::chrono::duration<double>(clock_type::now() - begin).count();
  result.first_error = first_error.load();
  for (auto& worker : worker_results) {
    auto& local = worker.result;
    result.completed += local.completed;
    result.failures += local.failures;
    result.missing_objects += local.missing_objects;
    result.duplicate_entries += local.duplicate_entries;
    result.returned_entries += local.returned_entries;
    result.probe_matching_records += local.probe_matching_records;
    result.probe_distance_computations += local.probe_distance_computations;
    result.recall_hits += local.recall_hits;
    result.recall_denominator += local.recall_denominator;
    result.rerank_unmapped += local.rerank_unmapped;
    result.latency_us.insert(result.latency_us.end(),
                             local.latency_us.begin(), local.latency_us.end());
    if (result.first_oid.empty() && !local.first_oid.empty()) {
      result.first_oid = local.first_oid;
    }
    result.tree_stats_probes += local.tree_stats_probes;
    result.tree_primary_candidate_count += local.tree_primary_candidate_count;
    result.tree_expanded_subtree_count += local.tree_expanded_subtree_count;
    result.tree_pruned_distance_count += local.tree_pruned_distance_count;
    result.tree_pruned_residual_count += local.tree_pruned_residual_count;
    result.tree_opened_onode_count += local.tree_opened_onode_count;
    result.tree_candidates_seen += local.tree_candidates_seen;
    result.tree_containment_violations += local.tree_containment_violations;
    result.selective_final_m_sum += local.selective_final_m_sum;
    result.selective_stages_sum += local.selective_stages_sum;
    for (const auto& [final_m, count] : local.selective_final_m_histogram) {
      result.selective_final_m_histogram[final_m] += count;
    }
    result.osd_requests += local.osd_requests;
    result.osd_query_ops += local.osd_query_ops;
  }
  return result;
}

void print_fanout_result(const options_t& options, const fanout_query_result_t& result)
{
  const double recall = result.recall_denominator == 0 ? 0.0 :
    static_cast<double>(result.recall_hits) /
      static_cast<double>(result.recall_denominator);
  std::cout << std::fixed << std::setprecision(6)
            << "FANOUT_QUERY mode=" << options.fanout_mode
            << " probe_pg_count=" << options.m
            << " subrequests_per_logical_query=" << options.m
            << " completed=" << result.completed
            << " failures=" << result.failures
            << " first_error=" << result.first_error
            << " missing_objects=" << result.missing_objects
            << " duplicate_entries=" << result.duplicate_entries
            << " seconds=" << result.seconds
            << " logical_qps=" << static_cast<double>(result.completed) /
                 (result.seconds > 0 ? result.seconds : 1)
            << " logical_latency_us_avg=" << (result.latency_us.empty() ? 0.0 :
                 std::accumulate(result.latency_us.begin(), result.latency_us.end(), 0.0) /
                   result.latency_us.size())
            << " logical_latency_us_p50=" << percentile(result.latency_us, 0.50)
            << " logical_latency_us_p95=" << percentile(result.latency_us, 0.95)
            << " logical_latency_us_p99=" << percentile(result.latency_us, 0.99)
            << " reply_bytes_per_query="
            << average(result.reply_bytes, result.completed)
            << " reply_vector_bytes_per_query="
            << average(result.reply_vector_bytes, result.completed)
            << " avg_matching_records_per_pg="
            << average(result.probe_matching_records, result.completed * options.m)
            << " avg_distance_computations_per_pg="
            << average(result.probe_distance_computations, result.completed * options.m)
            << " avg_total_matching_records_per_query="
            << average(result.probe_matching_records, result.completed)
            << " avg_total_distance_computations_per_query="
            << average(result.probe_distance_computations, result.completed)
            << " recall_at_top_k=" << recall
            << " rerank_top_n=" << options.rerank_top_n
            << " rerank_unmapped=" << result.rerank_unmapped
            << " avg_returned_results=" << average(result.returned_entries, result.completed)
            << " query_batch_size=" << options.query_batch_size
            << " osd_requests=" << result.osd_requests
            << " osd_query_ops=" << result.osd_query_ops
            << " ops_per_request="
            << average(result.osd_query_ops, result.osd_requests)
            << " osd_requests_per_query="
            << average(result.osd_requests, result.completed);
  if (options.selective) {
    // Selective fan-out wins when recall holds near the one-shot baseline
    // while avg_final_m sits well below --m. avg_stages is the cost: each
    // stage is one more round-trip, so p50 rises even as candidates fall.
    // Under the default --selective-stop never both follow the schedule.
    std::cout << " selective_schedule=" << options.selective_schedule
              << " selective_stop=" << options.selective_stop
              << " avg_final_m="
              << average(result.selective_final_m_sum, result.completed)
              << " avg_stages="
              << average(result.selective_stages_sum, result.completed)
              << " final_m_histogram=[";
    bool first = true;
    for (const auto& [final_m, count] : result.selective_final_m_histogram) {
      if (!first) std::cout << ',';
      first = false;
      std::cout << final_m << ':' << count;
    }
    std::cout << ']';
  }
  if (result.tree_stats_probes > 0) {
    // Averages are per logical query (i.e. summed across the --m probes
    // of one query, matching avg_total_*_per_query's own convention
    // above), not per-probe -- see fanout_query_result_t's tree_* field
    // comments.
    std::cout
        << " tree_primary_candidates_per_query="
        << average(result.tree_primary_candidate_count, result.completed)
        << " tree_expanded_subtrees_per_query="
        << average(result.tree_expanded_subtree_count, result.completed)
        << " tree_pruned_distance_per_query="
        << average(result.tree_pruned_distance_count, result.completed)
        << " tree_pruned_residual_per_query="
        << average(result.tree_pruned_residual_count, result.completed)
        << " tree_containment_violations="
        << result.tree_containment_violations
        << " tree_opened_onodes_per_query="
        << average(result.tree_opened_onode_count, result.completed)
        << " tree_candidates_seen_per_query="
        << average(result.tree_candidates_seen, result.completed);
  }
  // avg_inflight is the time-average outstanding-op count per worker: it is
  // what shows the depth was actually held, and slot_idle_ratio how much of
  // the configured depth went unused. avg_queue_wait_us is the part of
  // end-to-end latency a query spent waiting for a slot; avg_active_us is
  // the rest, which still contains server-side queueing.
  {
    const double avg_inflight = result.pipeline_wall_us > 0 ?
        result.inflight_integral_us / result.pipeline_wall_us : 0.0;
    std::cout
        << " io_depth=" << options.io_depth
        << " avg_queue_wait_us="
        << average(result.queue_wait_us_sum, result.completed)
        << " avg_active_us="
        << average(result.active_us_sum, result.completed)
        << " avg_inflight=" << avg_inflight
        << " max_inflight=" << result.max_inflight;
    if (options.io_depth > 0) {
      std::cout << " slot_idle_ratio="
                << (1.0 - avg_inflight / static_cast<double>(options.io_depth));
    }
  }
  std::cout << '\n';
}

void print_load_result(const load_result_t& result)
{
  std::cout << std::fixed << std::setprecision(6)
            << "LOAD completed=" << result.completed
            << " failures=" << result.failures
            << " seconds=" << result.seconds
            << " actual_put_targets_per_vector="
            << average(result.put_targets, result.completed + result.failures)
            << " rank_skipped=" << result.rank_skipped
            << " target_violations=" << result.target_violations
            << " vector_object_count=" << result.vector_oids.size() << '\n'
            << "PUT_OID_EXAMPLE " << result.first_oid << '\n';
}

void print_query_result(const options_t& options,
                        const query_result_t& result)
{
  const double qps = result.seconds == 0 ? 0 : result.completed / result.seconds;
  const double latency_avg = result.latency_us.empty() ? 0 :
    std::accumulate(result.latency_us.begin(), result.latency_us.end(), 0.0) /
    result.latency_us.size();
  const double recall = result.recall_denominator == 0 ? 0 :
    static_cast<double>(result.recall_hits) / result.recall_denominator;
  const std::string name = options.routing_mode == "pg-container" ?
    "PG-level one-PG-container exact scan" :
    "Object-level one-object exact scan (B_total=7)";

  std::cout << std::fixed << std::setprecision(6)
            << "RESULT_NAME " << name << '\n'
            << "QUERY completed=" << result.completed
            << " failures=" << result.failures
            << " missing_objects=" << result.missing_objects
            << " empty_results=" << result.empty_results
            << " unsorted_results=" << result.unsorted_results
            << " zero_counter_results=" << result.zero_counter_results
            << " seconds=" << result.seconds
            << " logical_qps=" << qps
            << " latency_us_avg=" << latency_avg
            << " latency_us_p50=" << percentile(result.latency_us, 0.50)
            << " latency_us_p95=" << percentile(result.latency_us, 0.95)
            << " latency_us_p99=" << percentile(result.latency_us, 0.99)
            << " recall_at_" << options.top_k << "=" << recall
            << " avg_returned_results=" << average(result.returned_entries, result.completed)
            << " pg_probes_per_query=" << average(result.pg_probes, result.completed)
            << " object_probes_per_query=" << average(result.object_probes, result.completed)
            << " avg_matching_records=" << average(result.matching_records, result.completed)
            << " avg_distance_computations=" << average(result.distance_computations, result.completed)
            << '\n'
            << "QUERY_OID_EXAMPLE " << result.first_oid << '\n'
            << "CSV," << options.routing_mode << ',' << options.pool << ','
            << options.query_concurrency << ',' << result.completed << ','
            << result.seconds << ',' << qps << ',' << latency_avg << ','
            << percentile(result.latency_us, 0.50) << ','
            << percentile(result.latency_us, 0.95) << ','
            << percentile(result.latency_us, 0.99) << ',' << recall << ','
            << average(result.returned_entries, result.completed) << ','
            << average(result.pg_probes, result.completed) << ','
            << average(result.object_probes, result.completed) << ','
            << average(result.matching_records, result.completed) << ','
            << average(result.distance_computations, result.completed) << ','
            << result.failures << ',' << result.missing_objects << '\n';
}


// --full-pg-scan: current PG -> one probe per vector object in it, built
// once from --object-list with the same locator a query would use.
std::map<uint32_t, std::vector<pg_lsh::query_probe_t>> g_full_pg_objects;

// Parses a `rados ls` line of the index into (bucket ordinal, sub_oid).
bool parse_vector_oid(const options_t& options, const std::string& line,
                      std::string *oid_name, uint32_t *partition,
                      std::string *sub_oid)
{
  std::string prefix = librados::vector_placement::make_pg_lsh_v0_oid(
      options.bucket, options.index, 0).name;
  prefix.pop_back();  // ".../pg_0" -> ".../pg_"
  *oid_name = line.substr(0, line.find('\t'));
  if (oid_name->compare(0, prefix.size(), prefix) != 0) return false;
  const auto partition_end = oid_name->find('/', prefix.size());
  const char *first = oid_name->data() + prefix.size();
  const char *last = oid_name->data() +
    (partition_end == std::string::npos ? oid_name->size() : partition_end);
  if (std::from_chars(first, last, *partition).ptr != last) return false;
  *sub_oid = partition_end == std::string::npos
    ? std::string() : oid_name->substr(partition_end + 1);
  return true;
}

int build_full_pg_objects(const options_t& options,
                          pg_lsh::locator_cache_t *cache)
{
  std::ifstream in(options.object_list_path);
  if (!in) {
    std::cerr << "error: cannot open " << options.object_list_path << '\n';
    return -ENOENT;
  }
  std::string line, oid_name, sub_oid;
  uint32_t partition = 0;
  uint64_t objects = 0;
  while (std::getline(in, line)) {
    if (!parse_vector_oid(options, line, &oid_name, &partition, &sub_oid)) {
      continue;
    }
    pg_lsh::query_probe_t probe;
    const int r = cache->bucket_locator(partition, &probe.locator_key,
                                        &probe.pg);
    if (r < 0) return r;
    probe.oid = object_t(oid_name);
    probe.placement_key =
      librados::vector_placement::pg_lsh_v0_placement_key(partition);
    probe.sub_oid_name = sub_oid;
    g_full_pg_objects[probe.pg].push_back(std::move(probe));
    ++objects;
  }
  std::cerr << "FULL_PG_SCAN objects=" << objects
            << " pgs=" << g_full_pg_objects.size() << '\n';
  return objects ? 0 : -ENOENT;
}

// Replaces each routed PG's probe(s) with one probe per object in that PG,
// keeping the routed PG order (so a PG's probes stay adjacent).
void expand_full_pg_scan(const options_t& options,
                         std::vector<pg_lsh::query_probe_t> *probes)
{
  if (!options.full_pg_scan) return;
  std::vector<pg_lsh::query_probe_t> out;
  std::unordered_set<uint32_t> seen;
  for (const auto& probe : *probes) {
    if (!seen.insert(probe.pg).second) continue;
    auto it = g_full_pg_objects.find(probe.pg);
    if (it == g_full_pg_objects.end()) continue;
    out.insert(out.end(), it->second.begin(), it->second.end());
  }
  *probes = std::move(out);
}

} // anonymous namespace

// object-census: scans each listed vector object once, through the
// locator a client would use for its partition now, with a zero query and
// local_top_k 1. The OSD's distance computation count is then the number
// of records the object holds, and phys_pg is where that locator lands in
// the current OSDMap. A partition whose locator no longer reaches its
// object reads back as missing.
int run_object_census(const options_t& options,
                      const pg_lsh::index_config_t& config,
                      librados::IoCtx& ioctx,
                      pg_lsh::locator_cache_t *cache)
{
  std::ifstream in(options.object_list_path);
  if (!in) {
    std::cerr << "error: cannot open " << options.object_list_path << '\n';
    return 2;
  }
  std::string prefix = librados::vector_placement::make_pg_lsh_v0_oid(
      options.bucket, options.index, 0).name;
  prefix.pop_back();  // ".../pg_0" -> ".../pg_"
  // Encoded like a real query: an SQ8 index rejects a float32 query.
  const auto zero_query = vector_buffer_typed(
    std::vector<float>(config.dimension, 0.0f), config.data_type,
    options.quantize_scale);

  uint64_t objects = 0, skipped = 0, failed = 0, missing = 0, records = 0;
  std::cout << "oid,partition,sub_oid,phys_pg,r,op_rval,records,locator\n";
  std::string line;
  while (std::getline(in, line)) {
    // `rados ls` prints the oid, then the locator after a tab if one is set.
    const std::string oid_name = line.substr(0, line.find('\t'));
    if (oid_name.compare(0, prefix.size(), prefix) != 0) {
      ++skipped;
      continue;
    }
    const auto partition_end = oid_name.find('/', prefix.size());
    uint32_t partition = 0;
    const char *first = oid_name.data() + prefix.size();
    const char *last = oid_name.data() +
      (partition_end == std::string::npos ? oid_name.size() : partition_end);
    if (std::from_chars(first, last, partition).ptr != last) {
      ++skipped;
      continue;
    }
    const std::string sub_oid = partition_end == std::string::npos
      ? std::string() : oid_name.substr(partition_end + 1);
    ++objects;

    std::string locator_key;
    uint32_t phys_pg = std::numeric_limits<uint32_t>::max();
    uint32_t bucket_pg = 0;
    int r = options.bucket_locators
      ? cache->bucket_locator(partition, &locator_key, &bucket_pg)
      : cache->locator_for_pg(partition, &locator_key);
    if (r == 0) {
      r = ioctx.get_object_pg_hash_position2(locator_key, &phys_pg);
    }
    int op_rval = 0;
    uint64_t object_records = 0;
    if (r == 0) {
      // Bucket mode: oid carries the bucket ordinal; the PG is the one
      // RADOS maps the bucket's locator to.
      const uint32_t probe_pg =
        options.bucket_locators ? bucket_pg : partition;
      const pg_lsh::query_probe_t probe = {
        probe_pg, 0, 0, object_t(oid_name), locator_key,
        librados::vector_placement::pg_lsh_v0_placement_key(partition),
        sub_oid, options.partition_num,
      };
      // Census reads the object itself, never a PG band.
      ceph::rados::query_vectors_request_t req;
      req.bucket_name = options.bucket;
      req.index_name = options.index;
      req.data_type = config.data_type;
      req.distance_metric = config.distance_metric;
      req.dimension = config.dimension;
      req.local_top_k = 1;
      req.query_vector = zero_query;
      ceph::bufferlist reply;
      r = pg_lsh::query_sync(ioctx, probe, std::move(req), &reply, &op_rval);
      if (r == 0 && op_rval == 0) {
        ceph::rados::query_vectors_result_t result;
        try {
          auto p = reply.cbegin();
          decode(result, p);
          object_records = result.local_distance_computations;
        } catch (const ceph::buffer::error&) {
          r = -EIO;
        }
      }
    }
    if (r == -ENOENT || op_rval == -ENOENT) {
      ++missing;
    } else if (r < 0 || op_rval < 0) {
      ++failed;
    }
    records += object_records;
    std::cout << oid_name << ',' << partition << ',' << sub_oid << ','
              << (phys_pg == std::numeric_limits<uint32_t>::max()
                    ? -1 : static_cast<int64_t>(phys_pg))
              << ',' << r << ',' << op_rval << ',' << object_records
              << ',' << locator_key << '\n';
  }
  std::cerr << "CENSUS_TOTAL objects=" << objects << " missing=" << missing
            << " failed=" << failed << " records=" << records
            << " skipped=" << skipped << '\n';
  return failed == 0 ? 0 : 1;
}

// placement-audit: offline, --d 1 only. For every base vector, the object
// it was written to sits under the locator a pool with pg_num == the
// index's address space gave it (partition mode: always). Once the pool
// has --audit-pg-num N PGs that object lives in PG fold(hash, N). A fresh
// index routed at N would send the vector to its first write PG at N.
// in_routed_pg counts vectors where the two agree. reachable counts
// vectors whose partition's locator, recomputed by a client against the
// N-PG pool, is still the one the object was written under.
int run_placement_audit(const options_t& options,
                        const pg_lsh::index_config_t& config,
                        const float_dataset_t& base)
{
  namespace vp = librados::vector_placement;
  const uint32_t space =
    config.partition_num != 0 ? config.partition_num : options.pg_num;
  const uint32_t audit_pg_num = options.audit_pg_num;
  const std::string bucket_hash = vp::hash_string(options.bucket);
  const std::string index_hash = vp::hash_string(options.index);

  // Written-under locator hash per partition, and the one a client would
  // recompute against an audit_pg_num pool.
  std::vector<std::optional<uint32_t>> written(space), recomputed(space);
  uint32_t written_found = 0, recomputed_found = 0;
  const uint32_t recompute_space =
    config.partition_num != 0 ? 0 : std::min(space, audit_pg_num);
  for (uint32_t salt = 0;
       (written_found < space || recomputed_found < recompute_space) &&
       salt < 100000000U;
       ++salt) {
    const uint32_t hash = pg_lsh::partition_locator_hash(
        pg_lsh::pg_lsh_locator_key(options.pool, bucket_hash, index_hash,
                                   salt));
    const uint32_t w = hash & (space - 1);
    if (!written[w]) {
      written[w] = hash;
      ++written_found;
    }
    if (recompute_space != 0) {
      const uint32_t live = pg_lsh::fold_hash_to_pg(hash, audit_pg_num);
      if (live < space && !recomputed[live]) {
        recomputed[live] = hash;
        ++recomputed_found;
      }
    }
  }
  if (written_found != space) {
    std::cerr << "error: locator scan incomplete\n";
    return 1;
  }

  uint64_t in_routed_pg = 0, reachable = 0;
  std::vector<uint64_t> per_pg(audit_pg_num, 0);
  for (const auto& row : base.rows) {
    const auto bl = vector_buffer(row);
    std::vector<vp::pg_lsh_v0_group_t> groups;
    if (pg_lsh::exact_lsh_groups(bl, config, &groups) < 0) {
      std::cerr << "error: lsh groups\n";
      return 1;
    }
    const uint32_t w =
      vp::pg_lsh_v0_select_write_pgs(groups, space, config.seed, 1).front();
    const uint32_t routed =
      vp::pg_lsh_v0_select_write_pgs(groups, audit_pg_num, config.seed, 1)
        .front();
    const uint32_t phys = pg_lsh::fold_hash_to_pg(*written[w], audit_pg_num);
    ++per_pg[phys];
    if (phys == routed) ++in_routed_pg;
    if (config.partition_num != 0 ||
        (recomputed[w] && *recomputed[w] == *written[w])) {
      ++reachable;
    }
  }
  const uint64_t max_pg = *std::max_element(per_pg.begin(), per_pg.end());
  const uint64_t nonempty = std::count_if(
      per_pg.begin(), per_pg.end(), [](uint64_t n) { return n != 0; });
  std::cout << std::fixed << std::setprecision(4)
            << "AUDIT space=" << space << " audit_pg_num=" << audit_pg_num
            << " vectors=" << base.rows.size()
            << " in_routed_pg=" << in_routed_pg << " ("
            << double(in_routed_pg) / base.rows.size() << ")"
            << " reachable=" << reachable << " ("
            << double(reachable) / base.rows.size() << ")"
            << " nonempty_pgs=" << nonempty
            << " max_pg_vectors=" << max_pg
            << " mean_pg_vectors="
            << double(base.rows.size()) / audit_pg_num << '\n';
  return 0;
}

// --operation route-sim: what a pivot load + full-PG fanout query would
// give, computed without a cluster. With pivot routing every PG holds one
// object that the OSD scans in full, so recall@k and the candidate count
// depend only on where the vectors were placed and which M PGs a query
// probes. Placement (nearest PG, d=1) and query ranking both call
// pivot_rank_pgs(), the function load_vectors() and the query paths use.
int run_route_sim(const options_t& options, const float_dataset_t& base,
                  const float_dataset_t& queries,
                  const ivec_dataset_t& groundtruth)
{
  std::vector<uint32_t> m_list;
  {
    std::stringstream ss(options.sim_m_list);
    std::string item;
    while (std::getline(ss, item, ',')) {
      if (item.empty()) continue;
      const long v = std::atol(item.c_str());
      if (v <= 0 || static_cast<uint64_t>(v) > options.pg_num) {
        std::cerr << "error: --sim-m-list value out of range: " << item << '\n';
        return 2;
      }
      m_list.push_back(static_cast<uint32_t>(v));
    }
  }
  if (m_list.empty()) {
    std::cerr << "error: empty --sim-m-list\n";
    return 2;
  }

  const auto model_begin = clock_type::now();
  const PivotModel model = build_pivot_model(
      base, options.pg_num, options.pivot_build_sample,
      options.pivot_refine_iters, options.pivot_spherical,
      options.pivot_balance_eps);
  const double model_seconds = std::chrono::duration<double>(
      clock_type::now() - model_begin).count();
  std::cout << "PIVOT_MODEL mode=pivot seed=" << options.seed
            << " sample_count=" << model.sample_count
            << " representative_count=" << model.reps.size()
            << " dim=" << model.dim
            << " refine_iters=" << options.pivot_refine_iters
            << " spherical=" << (options.pivot_spherical ? 1 : 0)
            << " balance_eps=" << options.pivot_balance_eps
            << " build_seconds=" << std::fixed << std::setprecision(3)
            << model_seconds << std::setprecision(6)
            << " digest=" << std::hex << std::setw(16) << std::setfill('0')
            << pivot_model_digest(model.reps)
            << std::dec << std::setfill(' ') << '\n';
  if (model.reps.size() != options.pg_num) {
    std::cerr << "error: pivot model built " << model.reps.size()
              << " representatives, expected pg_num=" << options.pg_num << '\n';
    return 1;
  }

  // Placement: load_vectors() writes each row to the first --d PGs of
  // pivot_rank_pgs(); route-sim models d=1.
  const auto place_begin = clock_type::now();
  std::vector<uint32_t> pg_of(base.rows.size());
  if (options.sim_place_cap_eps > 0.0) {
    std::vector<size_t> a(base.rows.size());
    std::vector<float> d(base.rows.size());
    capped_assign(base.rows.size(), [&](size_t i) { return base.rows[i].data(); },
                  model, options.sim_place_cap_eps, &a, &d);
    for (size_t row = 0; row < a.size(); ++row) pg_of[row] = a[row];
  } else {
    parallel_for(base.rows.size(), [&](size_t b, size_t e) {
      for (size_t row = b; row < e; ++row) {
        pg_of[row] = pivot_rank_pgs(base.rows[row].data(), model.dim, model, 1)[0];
      }
    });
  }
  std::vector<uint64_t> pg_size(options.pg_num, 0);
  for (const uint32_t pg : pg_of) ++pg_size[pg];
  std::vector<std::vector<uint32_t>> members(options.pg_num);
  for (uint32_t pg = 0; pg < options.pg_num; ++pg) members[pg].reserve(pg_size[pg]);
  for (size_t row = 0; row < pg_of.size(); ++row) {
    members[pg_of[row]].push_back(static_cast<uint32_t>(row));
  }

  // Query-side PG statistics come from the build sample only (the same
  // stride rows build_pivot_model() used), grouped by where they were
  // actually placed -- what a client could know without reading the PGs.
  std::vector<std::vector<uint32_t>> sampled(options.pg_num);
  {
    const size_t sample_limit = std::min<size_t>(
        base.rows.size(), std::max<size_t>(1, options.pivot_build_sample));
    const size_t stride = std::max<size_t>(1, base.rows.size() / sample_limit);
    size_t taken = 0;
    for (size_t i = 0; i < base.rows.size() && taken < sample_limit;
         i += stride, ++taken) {
      sampled[pg_of[i]].push_back(static_cast<uint32_t>(i));
    }
  }
  std::vector<float> radius(options.pg_num, 0.0f);
  if (options.sim_radius_alpha != 0.0) {
    parallel_for(options.pg_num, [&](size_t b, size_t e) {
      for (size_t pg = b; pg < e; ++pg) {
        std::vector<float> d;
        for (const uint32_t id : sampled[pg]) {
          d.push_back(std::sqrt(l2_dist_sq(base.rows[id].data(),
                                           model.reps[pg].data(), model.dim)));
        }
        if (d.empty()) continue;
        std::sort(d.begin(), d.end());
        radius[pg] = d[std::min(d.size() - 1, static_cast<size_t>(
            options.sim_radius_q * (d.size() - 1) + 0.5))];
      }
    });
  }
  // Multi-rep: per PG, farthest-first then 10 Lloyd passes over its sampled
  // members (renormalised under --pivot-spherical). A PG with fewer sampled
  // members than R uses them all; one with none keeps its model rep.
  std::vector<std::vector<std::vector<float>>> sub_reps(options.pg_num);
  const uint32_t R = std::max<uint32_t>(1, options.sim_multi_rep);
  if (R > 1) {
    parallel_for(options.pg_num, [&](size_t b, size_t e) {
      for (size_t pg = b; pg < e; ++pg) {
        const auto& mem = sampled[pg];
        auto& reps = sub_reps[pg];
        if (mem.empty()) { reps.push_back(model.reps[pg]); continue; }
        const int dim = model.dim;
        // FF seed: start from the member nearest the PG rep.
        size_t first = 0;
        float fd = std::numeric_limits<float>::max();
        for (size_t i = 0; i < mem.size(); ++i) {
          const float d = l2_dist_sq(base.rows[mem[i]].data(), model.reps[pg].data(), dim);
          if (d < fd) { fd = d; first = i; }
        }
        reps.push_back(base.rows[mem[first]]);
        std::vector<float> md(mem.size());
        for (size_t i = 0; i < mem.size(); ++i)
          md[i] = l2_dist_sq(base.rows[mem[i]].data(), reps[0].data(), dim);
        while (reps.size() < std::min<size_t>(R, mem.size())) {
          size_t bi = 0;
          for (size_t i = 1; i < mem.size(); ++i) if (md[i] > md[bi]) bi = i;
          if (md[bi] <= 0.0f) break;
          reps.push_back(base.rows[mem[bi]]);
          for (size_t i = 0; i < mem.size(); ++i)
            md[i] = std::min(md[i], l2_dist_sq(base.rows[mem[i]].data(), reps.back().data(), dim));
        }
        double target = 0.0;
        if (options.pivot_spherical) {
          for (const uint32_t id : mem) {
            double sq = 0.0;
            for (int j = 0; j < dim; ++j) sq += double(base.rows[id][j]) * base.rows[id][j];
            target += std::sqrt(sq);
          }
          target /= mem.size();
        }
        const size_t kr = reps.size();
        for (int it = 0; it < 10 && kr > 1; ++it) {
          std::vector<double> sum(kr * dim, 0.0);
          std::vector<size_t> cnt(kr, 0);
          for (const uint32_t id : mem) {
            size_t best = 0;
            float bd = std::numeric_limits<float>::max();
            for (size_t r = 0; r < kr; ++r) {
              const float d = l2_dist_sq(base.rows[id].data(), reps[r].data(), dim);
              if (d < bd) { bd = d; best = r; }
            }
            ++cnt[best];
            for (int j = 0; j < dim; ++j) sum[best * dim + j] += base.rows[id][j];
          }
          for (size_t r = 0; r < kr; ++r) {
            if (cnt[r] == 0) continue;
            double scale = 1.0;
            if (options.pivot_spherical) {
              double sq = 0.0;
              for (int j = 0; j < dim; ++j) { const double m = sum[r * dim + j] / cnt[r]; sq += m * m; }
              if (sq > 0.0) scale = target / std::sqrt(sq);
            }
            for (int j = 0; j < dim; ++j)
              reps[r][j] = static_cast<float>(sum[r * dim + j] / cnt[r] * scale);
          }
        }
      }
    });
  }
  const bool custom_rank = options.sim_radius_alpha != 0.0 || R > 1;
  // Query ranking: plain pivot_rank_pgs() unless a query-side variant is on.
  auto rank_query = [&](const float *qv) {
    if (!custom_rank) {
      return pivot_rank_pgs(qv, model.dim, model, options.pg_num);
    }
    std::vector<std::pair<float, uint32_t>> sc(options.pg_num);
    for (uint32_t pg = 0; pg < options.pg_num; ++pg) {
      float d2;
      if (R > 1) {
        d2 = std::numeric_limits<float>::max();
        for (const auto& r : sub_reps[pg]) d2 = std::min(d2, l2_dist_sq(qv, r.data(), model.dim));
      } else {
        d2 = l2_dist_sq(qv, model.reps[pg].data(), model.dim);
      }
      sc[pg] = {std::sqrt(d2) - static_cast<float>(options.sim_radius_alpha) * radius[pg], pg};
    }
    std::sort(sc.begin(), sc.end());
    std::vector<uint32_t> out(options.pg_num);
    for (uint32_t i = 0; i < options.pg_num; ++i) out[i] = sc[i].second;
    return out;
  };
  std::cout << "SIM_VARIANT place_cap_eps=" << options.sim_place_cap_eps
            << " balance_eps=" << options.pivot_balance_eps
            << " radius_alpha=" << options.sim_radius_alpha
            << " radius_q=" << options.sim_radius_q
            << " multi_rep=" << R << '\n';
  const double place_seconds = std::chrono::duration<double>(
      clock_type::now() - place_begin).count();

  {
    std::vector<uint64_t> sorted = pg_size;
    std::sort(sorted.begin(), sorted.end());
    const double n = static_cast<double>(base.rows.size());
    const double mean = n / options.pg_num;
    double var = 0.0, gini_num = 0.0;
    size_t empty = 0;
    for (size_t i = 0; i < sorted.size(); ++i) {
      const double d = static_cast<double>(sorted[i]) - mean;
      var += d * d;
      gini_num += (2.0 * (i + 1) - sorted.size() - 1) * sorted[i];
      if (sorted[i] == 0) ++empty;
    }
    var /= sorted.size();
    const auto q = [&](double f) {
      return sorted[std::min(sorted.size() - 1,
                             static_cast<size_t>(f * (sorted.size() - 1) + 0.5))];
    };
    std::cout << std::fixed << std::setprecision(6)
              << "PG_SIZES vectors=" << base.rows.size()
              << " empty=" << empty
              << " min=" << sorted.front() << " p50=" << q(0.5)
              << " p90=" << q(0.9) << " p99=" << q(0.99)
              << " max=" << sorted.back()
              << " max_over_mean=" << sorted.back() / mean
              << " cv=" << std::sqrt(var) / mean
              << " gini=" << gini_num / (sorted.size() * n)
              << " place_seconds=" << std::setprecision(3) << place_seconds
              << '\n';
  }

  // Per query: rank every PG, then for each M the candidate count is the
  // sum of the first M PG sizes, and a ground-truth neighbour is found iff
  // its PG is among them (a full-PG scan returns the exact top-k of the
  // union). gt_rank records, for each ground-truth neighbour, the position
  // of its PG in the query's ranking -- the M that query would need.
  const size_t qn = queries.rows.size();
  const size_t topk = options.top_k;
  std::vector<std::vector<uint64_t>> cand(m_list.size(),
                                          std::vector<uint64_t>(qn, 0));
  std::vector<std::vector<uint32_t>> hits(m_list.size(),
                                          std::vector<uint32_t>(qn, 0));
  // topk_hits is what the bench itself scores: ground-truth ids among the
  // exact top-k (float32 L2) of the scanned union. It sits below hits when
  // the union holds vectors tied with or closer than a ground-truth one
  // (duplicates, or ground truth computed in another precision).
  std::vector<std::vector<uint32_t>> topk_hits(m_list.size(),
                                               std::vector<uint32_t>(qn, 0));
  const uint32_t max_m = *std::max_element(m_list.begin(), m_list.end());
  std::vector<uint32_t> gt_rank(qn * topk, std::numeric_limits<uint32_t>::max());
  std::atomic<uint64_t> bad_gt{0};
  const auto query_begin = clock_type::now();
  parallel_for(qn, [&](size_t b, size_t e) {
    std::vector<uint32_t> pos(options.pg_num);
    for (size_t qi = b; qi < e; ++qi) {
      const auto ranked = rank_query(queries.rows[qi].data());
      for (size_t r = 0; r < ranked.size(); ++r) pos[ranked[r]] = r;
      for (size_t mi = 0; mi < m_list.size(); ++mi) {
        uint64_t c = 0;
        for (size_t r = 0; r < m_list[mi]; ++r) c += pg_size[ranked[r]];
        cand[mi][qi] = c;
      }
      const auto& truth = groundtruth.rows[qi];
      {
        // Scan PGs in rank order, keeping the running top-k; read it off at
        // every M checkpoint.
        using cand_t = std::pair<float, uint32_t>;
        std::vector<cand_t> heap;  // max-heap on (dist, id)
        heap.reserve(topk + 1);
        const float *qv = queries.rows[qi].data();
        for (uint32_t r = 0; r < max_m; ++r) {
          for (const uint32_t id : members[ranked[r]]) {
            const cand_t c{l2_dist_sq(qv, base.rows[id].data(), model.dim), id};
            if (heap.size() < topk) {
              heap.push_back(c);
              std::push_heap(heap.begin(), heap.end());
            } else if (c < heap.front()) {
              std::pop_heap(heap.begin(), heap.end());
              heap.back() = c;
              std::push_heap(heap.begin(), heap.end());
            }
          }
          for (size_t mi = 0; mi < m_list.size(); ++mi) {
            if (m_list[mi] != r + 1) continue;
            uint32_t h = 0;
            for (size_t t = 0; t < topk && t < truth.size(); ++t) {
              for (const auto& c : heap) {
                if (c.second == truth[t]) { ++h; break; }
              }
            }
            topk_hits[mi][qi] = h;
          }
        }
      }
      for (size_t t = 0; t < topk && t < truth.size(); ++t) {
        const int64_t id = truth[t];
        if (id < 0 || static_cast<size_t>(id) >= pg_of.size()) {
          ++bad_gt;
          continue;
        }
        const uint32_t rank = pos[pg_of[id]];
        gt_rank[qi * topk + t] = rank;
        for (size_t mi = 0; mi < m_list.size(); ++mi) {
          if (rank < m_list[mi]) ++hits[mi][qi];
        }
      }
    }
  });
  const double query_seconds = std::chrono::duration<double>(
      clock_type::now() - query_begin).count();
  if (bad_gt != 0) {
    std::cerr << "error: " << bad_gt.load()
              << " ground-truth ids are outside --base\n";
    return 1;
  }

  for (size_t mi = 0; mi < m_list.size(); ++mi) {
    std::vector<uint64_t> c = cand[mi];
    std::sort(c.begin(), c.end());
    double csum = 0.0, hsum = 0.0, tsum = 0.0;
    for (size_t qi = 0; qi < qn; ++qi) {
      csum += cand[mi][qi];
      hsum += hits[mi][qi];
      tsum += topk_hits[mi][qi];
    }
    std::cout << std::fixed << std::setprecision(6)
              << "ROUTE_SIM m=" << m_list[mi]
              << " queries=" << qn
              << " recall_at_" << topk << "=" << tsum / (qn * topk)
              << " gt_reachable=" << hsum / (qn * topk)
              << " candidates_mean=" << std::setprecision(3) << csum / qn
              << " candidates_p50=" << c[c.size() / 2]
              << " candidates_p99=" << c[std::min(c.size() - 1, c.size() * 99 / 100)]
              << " candidates_max=" << c.back() << '\n';
  }
  {
    std::vector<uint32_t> r = gt_rank;
    std::sort(r.begin(), r.end());
    const auto q = [&](double f) {
      return r[std::min(r.size() - 1, static_cast<size_t>(f * (r.size() - 1)))];
    };
    std::cout << "GT_PG_RANK p50=" << q(0.5) << " p90=" << q(0.9)
              << " p95=" << q(0.95) << " p99=" << q(0.99)
              << " max=" << r.back()
              << " query_seconds=" << std::setprecision(3) << query_seconds
              << '\n';
  }
  return 0;
}

int main(int argc, char **argv)
{
  options_t options;
  if (!parse_options(argc, argv, &options)) {
    usage(std::cerr, argv[0]);
    return 2;
  }

  std::string error;
  auto base = read_fvecs(options.base_path, options.base_limit, &error);
  if (!base) {
    std::cerr << "error: " << error << '\n';
    return 2;
  }
  if (options.quantize == "int8") {
    options.quantize_scale = quantization_scale(*base);
  }
  if (options.rerank_top_n > 0) {
    if (!base) {
      std::cerr << "--rerank-top-n needs --base: the rerank rescores "
                   "against the original float32 vectors\n";
      return 2;
    }
    options.rerank = build_rerank_context(options, *base);
  }
  std::optional<float_dataset_t> queries;
  std::optional<ivec_dataset_t> groundtruth;
  std::vector<size_t> query_indices;
  if (options.operation != "load") {
    const bool use_index_list = !options.query_index_list_path.empty();
    // limit=0 reads the whole file: --query-index-list picks specific rows
    // out of the full set rather than "the first --query-limit rows", and
    // groundtruth (if requested) must be looked up by those same original
    // row indices, not by position in the subset.
    queries = read_fvecs(
        options.query_path, use_index_list ? 0 : options.query_limit,
        &error);
    if (!queries || queries->dimension != base->dimension) {
      std::cerr << "error: " << (queries ? "dimension mismatch" : error) << '\n';
      return 2;
    }
    if (use_index_list) {
      auto indices = read_index_list(options.query_index_list_path, &error);
      if (!indices) {
        std::cerr << "error: " << error << '\n';
        return 2;
      }
      query_indices = *indices;
      auto selected = select_rows(*queries, query_indices, &error);
      if (!selected) {
        std::cerr << "error: " << error << '\n';
        return 2;
      }
      queries = std::move(selected);
    }
    if (!options.groundtruth_path.empty()) {
      groundtruth = read_ivecs(
          options.groundtruth_path,
          use_index_list ? 0 : queries->rows.size(), &error);
      if (!groundtruth ||
          (!use_index_list && groundtruth->rows.size() < queries->rows.size())) {
        std::cerr << "error: " << (groundtruth ? "short ground truth" : error) << '\n';
        return 2;
      }
      if (use_index_list) {
        ivec_dataset_t selected_truth;
        selected_truth.rows.reserve(query_indices.size());
        for (const size_t index : query_indices) {
          if (index >= groundtruth->rows.size()) {
            std::cerr << "error: ground truth index out of range: "
                      << index << '\n';
            return 2;
          }
          selected_truth.rows.push_back(groundtruth->rows[index]);
        }
        groundtruth = std::move(selected_truth);
      }
    }
  }

  if (options.operation == "placement-audit") {
    return run_placement_audit(
        options, requested_config(options, *base), *base);
  }
  if (options.operation == "route-sim") {
    if (!groundtruth) {
      std::cerr << "error: route-sim needs --groundtruth\n";
      return 2;
    }
    return run_route_sim(options, *base, *queries, *groundtruth);
  }

  librados::Rados cluster;
  int r = cluster.init2(options.client_name.c_str(),
                        options.cluster_name.c_str(), 0);
  if (r == 0) r = cluster.conf_read_file(
      options.conf_path.empty() ? nullptr : options.conf_path.c_str());
  if (r == 0) r = cluster.connect();
  if (r < 0) {
    std::cerr << "error: cluster connection: " << r << " " << error_text(r) << '\n';
    return 1;
  }

  librados::IoCtx ioctx;
  r = cluster.ioctx_create(options.pool.c_str(), ioctx);
  if (r < 0) {
    std::cerr << "error: ioctx: " << r << " " << error_text(r) << '\n';
    return 1;
  }

  auto config = requested_config(options, *base);
  std::string invalid_field;
  // routing-mode=pivot keeps no server-side index config: the model is
  // determined by --base, --pivot-build-sample, --pivot-refine-iters,
  // --pg-num and --seed, so every invocation recomputes the same one --
  // compare the PIVOT_MODEL digest across runs to prove it.
  if (options.routing_mode != "pivot") {
    if (options.operation == "load" || options.operation == "both") {
      r = pg_lsh::create_index(ioctx, options.bucket, options.index,
                               config, pool_info(options), &invalid_field);
    } else {
      r = pg_lsh::load_index_config(ioctx, options.bucket, options.index,
                                    &config, &invalid_field);
      if (r == 0) {
        const auto requested = requested_config(options, *base);
        r = pg_lsh::compare_index_configs(requested, config, &invalid_field);
        // The stored config has no trees (runtime-only); attach the ones
        // this run retrained.
        config.trained_trees = requested.trained_trees;
      }
    }
    if (r < 0 || pg_lsh::validate_query_params(
          config, query_params(options), pool_info(options), &invalid_field) < 0) {
      std::cerr << "error: pg-lsh config field=" << invalid_field
                << " code=" << r << '\n';
      return 1;
    }
    print_training_derived_model(config);
  }

  std::optional<PivotModel> pivot_model;
  double pivot_model_seconds = 0.0;
  if (options.routing_mode == "pivot") {
    const auto model_begin = clock_type::now();
    pivot_model = build_pivot_model(*base, options.pg_num,
                                    options.pivot_build_sample,
                                    options.pivot_refine_iters,
                                    options.pivot_spherical,
                                    options.pivot_balance_eps);
    pivot_model_seconds = std::chrono::duration<double>(
        clock_type::now() - model_begin).count();
  }
  if (pivot_model) {
    std::cout << "PIVOT_MODEL mode=" << options.routing_mode
              << " seed=" << options.seed
              << " sample_count=" << pivot_model->sample_count
              << " representative_count=" << pivot_model->reps.size()
              << " dim=" << pivot_model->dim
              << " refine_iters=" << options.pivot_refine_iters
              << " spherical=" << (options.pivot_spherical ? 1 : 0)
              << " balance_eps=" << options.pivot_balance_eps
              << " build_seconds=" << std::fixed << std::setprecision(3)
              << pivot_model_seconds << std::setprecision(6)
              << " digest=" << std::hex << std::setw(16) << std::setfill('0')
              << pivot_model_digest(pivot_model->reps)
              << std::dec << std::setfill(' ') << '\n';
    if (pivot_model->reps.size() != options.pg_num) {
      std::cerr << "error: pivot model built " << pivot_model->reps.size()
                << " representatives, expected pg_num=" << options.pg_num << '\n';
      return 1;
    }
  }
  if (pivot_model && options.sim_place_cap_eps > 0.0 &&
      (options.operation == "load" || options.operation == "both")) {
    if (options.d != 1) {
      std::cerr << "error: --pivot-place-cap-eps needs --d 1\n";
      return 2;
    }
    const auto place_begin = clock_type::now();
    std::vector<size_t> a(base->rows.size());
    std::vector<float> dist(base->rows.size());
    capped_assign(base->rows.size(),
                  [&](size_t i) { return base->rows[i].data(); },
                  *pivot_model, options.sim_place_cap_eps, &a, &dist);
    pivot_model->placement.assign(a.begin(), a.end());
    std::vector<uint64_t> fill(options.pg_num, 0);
    uint64_t h = 1469598103934665603ULL;
    for (const uint32_t pg : pivot_model->placement) {
      ++fill[pg];
      h = (h ^ pg) * 1099511628211ULL;
    }
    std::cout << "PIVOT_PLACEMENT cap_eps=" << options.sim_place_cap_eps
              << " max_pg=" << *std::max_element(fill.begin(), fill.end())
              << " min_pg=" << *std::min_element(fill.begin(), fill.end())
              << " seconds=" << std::fixed << std::setprecision(3)
              << std::chrono::duration<double>(clock_type::now() - place_begin).count()
              << std::setprecision(6)
              << " digest=" << std::hex << std::setw(16) << std::setfill('0') << h
              << std::dec << std::setfill(' ') << '\n';
  }
  const PivotModel *pivot_model_ptr = pivot_model ? &*pivot_model : nullptr;

  auto locator_state = std::make_shared<pg_lsh::locator_state_t>();
  pg_lsh::locator_cache_t locator_cache(
      &ioctx, pool_info(options), options.pool, options.bucket,
      options.index, locator_state, options.partition_num,
      options.bucket_locators, options.k);
  r = locator_cache.precompute_all();
  if (r < 0) {
    std::cerr << "error: locator precompute: " << r << ' ' << error_text(r) << '\n';
    return 1;
  }

  std::cout << "CONFIG routing_mode=" << options.routing_mode
            << " placement_algorithm=pg-lsh-v0 pg_num=" << options.pg_num
            << " d=" << options.d << " m=" << options.m
            << " hamming_radius=" << options.hamming_radius
            << " distance_bucket_bits=" << options.distance_bucket_bits
            << " residual_bits=" << options.residual_bits
            << " distance_bucket_radius=" << options.distance_bucket_radius
            << " residual_hamming_radius=" << options.residual_hamming_radius
            << " probe_limit_per_pg=" << options.probe_limit_per_pg
            << " top_k=" << options.top_k
            << " quantize=" << options.quantize
            << " quantize_scale=" << options.quantize_scale
            << " rerank_top_n=" << options.rerank_top_n << '\n';

  if (options.operation == "object-census") {
    return run_object_census(options, config, ioctx, &locator_cache);
  }
  if (options.full_pg_scan) {
    r = build_full_pg_objects(options, &locator_cache);
    if (r < 0) {
      std::cerr << "error: full-pg-scan object list: " << r << '\n';
      return 1;
    }
  }
  if (options.operation == "load" || options.operation == "both") {
    const auto loaded = load_vectors(
        options, *base, cluster, locator_state, config, pivot_model_ptr);
    print_load_result(loaded);
    if (loaded.failures != 0 || loaded.completed != base->rows.size()) return 1;
  }
  if (options.operation == "query" || options.operation == "both") {
    if (options.warmup_rounds != 0) {
      const auto warmup = run_queries(
          options, *queries, nullptr, config, cluster, locator_state,
          options.warmup_rounds, 0, 0, false);
      std::cout << "WARMUP completed=" << warmup.completed
                << " failures=" << warmup.failures
                << " missing_objects=" << warmup.missing_objects
                << " empty_results=" << warmup.empty_results << '\n';
      if (warmup.failures != 0) return 1;
    }
    dest_pg_log_t dest_log;
    dest_pg_log_t *dest_log_ptr = nullptr;
    if (!options.destination_pg_log_path.empty()) {
      dest_log.out.open(options.destination_pg_log_path);
      if (!dest_log.out) {
        std::cerr << "error: cannot open "
                  << options.destination_pg_log_path << '\n';
        return 2;
      }
      dest_log.out << "ticket,query_index,pg\n";
      dest_log_ptr = &dest_log;
    }
    const auto measured = run_queries(
        options, *queries, groundtruth ? &*groundtruth : nullptr,
        config, cluster, locator_state, options.rounds,
        options.min_queries, options.min_seconds, true, dest_log_ptr);
    print_query_result(options, measured);
    if (measured.failures != 0) return 1;
  }
  if (options.operation == "fanout-query") {
    std::ofstream fanout_log;
    std::ofstream *fanout_log_ptr = nullptr;
    if (!options.fanout_log_path.empty()) {
      fanout_log.open(options.fanout_log_path);
      if (!fanout_log) {
        std::cerr << "error: cannot open " << options.fanout_log_path << '\n';
        return 2;
      }
      fanout_log_ptr = &fanout_log;
    }
    // A non-selective run is the single-stage case of the same loop: one
    // stage covering all m routed PGs, dispatched exactly as before.
    sf::policy_t policy;
    if (options.selective) {
      if (sf::parse_schedule(options.selective_schedule, &policy.schedule) < 0) {
        std::cerr << "error: bad --selective-schedule "
                  << options.selective_schedule << '\n';
        return 2;
      }
      policy.stop_mode = options.selective_stop == "tau-relative"
        ? sf::stop_mode_t::tau_relative_improvement
        : sf::stop_mode_t::never;
      policy.min_relative_improvement = options.selective_min_improvement;
    } else {
      policy.schedule = {options.m};
      policy.stop_mode = sf::stop_mode_t::never;
    }
    std::ofstream query_results_csv;
    std::ofstream *query_results_csv_ptr = nullptr;
    if (!options.query_results_csv_path.empty()) {
      query_results_csv.open(options.query_results_csv_path);
      if (!query_results_csv) {
        std::cerr << "error: cannot open " << options.query_results_csv_path
                  << '\n';
        return 2;
      }
      query_results_csv << "query_index,rank,entry_id,distance\n";
      query_results_csv_ptr = &query_results_csv;
    }
    std::ofstream selective_csv;
    std::ofstream *selective_csv_ptr = nullptr;
    if (!options.selective_csv_path.empty()) {
      selective_csv.open(options.selective_csv_path);
      if (!selective_csv) {
        std::cerr << "error: cannot open " << options.selective_csv_path << '\n';
        return 2;
      }
      selective_csv << sf::stage_csv_header() << '\n';
      selective_csv_ptr = &selective_csv;
    }
    if (options.warmup_rounds != 0 || options.warmup_queries != 0) {
      // Warmup writes no CSV: its rows would be indistinguishable from the
      // measured run's in the same file, and its latencies are cold.
      // min_queries with min_seconds 0 stops after exactly that many.
      const auto warmup = options.query_batch_size > 1
        ? run_batch_queries(
            options, *queries, nullptr, config, cluster, locator_state,
            options.warmup_rounds, 0, 0, false, pivot_model_ptr)
        : run_fanout_queries(
            options, *queries, nullptr, config, cluster, locator_state,
            options.warmup_rounds, options.warmup_queries, 0, false, nullptr,
            policy, nullptr, pivot_model_ptr);
      std::cout << "WARMUP completed=" << warmup.completed
                << " failures=" << warmup.failures
                << " missing_objects=" << warmup.missing_objects << '\n';
      if (warmup.failures != 0) return 1;
    }
    const auto measured = options.query_batch_size > 1
      ? run_batch_queries(
          options, *queries, groundtruth ? &*groundtruth : nullptr,
          config, cluster, locator_state, options.rounds,
          options.min_queries, options.min_seconds, true, pivot_model_ptr,
          query_results_csv_ptr)
      : run_fanout_queries(
          options, *queries, groundtruth ? &*groundtruth : nullptr,
          config, cluster, locator_state, options.rounds,
          options.min_queries, options.min_seconds, true, fanout_log_ptr,
          policy, selective_csv_ptr, pivot_model_ptr, query_results_csv_ptr);
    print_fanout_result(options, measured);
    if (measured.failures != 0) return 1;
  }
  return 0;
}
