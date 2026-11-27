#!/usr/bin/env python3
"""Experiment 2 (2026-10-21): CRUSH-style baseline for the pivot path.

--hash-placement (routing-mode pivot only):
  load   every --base row goes to PG = rjenkins("vec_<row>") & (pg_num - 1),
         i.e. the PG plain Ceph would give an object named after the vector
         (ceph_str_hash_rjenkins + ceph_stable_mod, pg_num a power of two).
         Same one-object-per-PG layout and OSD scan as the pivot arm.
  query  the placement carries no information about the query, so each query
         probes the first M PGs of its own pseudo-random PG permutation
         (seeded by --seed and the query index; identical in every run).
No pivot model is built or read: reps are zeros, only reps.size() == pg_num
is used. Query runs read only the first --base row (for the dimension).
"""
import sys
p = sys.argv[1]
s = open(p).read()


def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:80], s.count(a))
    s = s.replace(a, b)


rep('''  std::string pivot_model_file;
''', '''  std::string pivot_model_file;
  // Experiment 2 baseline: CRUSH-style hash placement + random probe order
  // instead of the pivot model (see hash_probe_pgs()).
  bool hash_placement = false;
''')
rep('OPT_PIVOT_BALANCE_EPS, OPT_PIVOT_MODEL_FILE,',
    'OPT_PIVOT_BALANCE_EPS, OPT_PIVOT_MODEL_FILE, OPT_HASH_PLACEMENT,')
rep('''    {"pivot-model-file", required_argument, nullptr, OPT_PIVOT_MODEL_FILE},
''', '''    {"pivot-model-file", required_argument, nullptr, OPT_PIVOT_MODEL_FILE},
    {"hash-placement", no_argument, nullptr, OPT_HASH_PLACEMENT},
''')
rep('''    case OPT_PIVOT_MODEL_FILE: options->pivot_model_file = optarg; break;
''', '''    case OPT_PIVOT_MODEL_FILE: options->pivot_model_file = optarg; break;
    case OPT_HASH_PLACEMENT: options->hash_placement = true; break;
''')
rep('''      << "  --pivot-model-file PATH: read the pivot model from PATH if it\\n"
''', '''      << "  --hash-placement: pivot mode with CRUSH-style placement instead of\\n"
      << "    the model: row i -> PG rjenkins(\\"vec_<i>\\") & (pg_num-1); each\\n"
      << "    query probes the first M PGs of a seeded random permutation.\\n"
      << "  --pivot-model-file PATH: read the pivot model from PATH if it\\n"
''')
rep('#include "include/rados/vector_ops.h"\n',
    '#include "include/rados/vector_ops.h"\n#include "include/ceph_hash.h"\n')

# helpers, right before pivot_rank_pgs()
rep('''std::vector<uint32_t> pivot_rank_pgs(const float *vec, int dim,
                                     const PivotModel& model, uint32_t keep_n)
{''', '''// --hash-placement: the PG plain Ceph would map an object "vec_<row>" to.
inline uint32_t hash_place_pg(uint64_t row, uint32_t pg_num)
{
  const std::string name = "vec_" + std::to_string(row);
  return ceph_str_hash_rjenkins(name.data(), name.size()) & (pg_num - 1);
}

// --hash-placement: hash placement says nothing about where a query's
// neighbours are, so the probe order is a random permutation of all PGs,
// fixed per (seed, query index); the first M are probed.
inline std::vector<uint32_t> hash_probe_pgs(uint64_t seed, size_t query_index,
                                            uint32_t pg_num, uint32_t m)
{
  std::vector<uint32_t> pgs(pg_num);
  std::iota(pgs.begin(), pgs.end(), 0u);
  std::mt19937_64 rng(seed * 0x9E3779B97F4A7C15ULL + query_index);
  const uint32_t keep = std::min(m, pg_num);
  for (uint32_t i = 0; i < keep; ++i) {
    std::uniform_int_distribution<uint32_t> pick(i, pg_num - 1);
    std::swap(pgs[i], pgs[pick(rng)]);
  }
  pgs.resize(keep);
  return pgs;
}

std::vector<uint32_t> pivot_rank_pgs(const float *vec, int dim,
                                     const PivotModel& model, uint32_t keep_n)
{''')

# load: placement vector is filled for hash mode -> skip the (meaningless) ranking
rep('''        if (options.routing_mode == "pivot") {
          const auto ranked = pivot_rank_pgs(
              base.rows[row].data(), pivot_model->dim, *pivot_model,
              options.pivot_probe_budget);
          std::vector<uint32_t> write_pgs(
              ranked.begin(),
              ranked.begin() + std::min<size_t>(options.d, ranked.size()));
          if (!pivot_model->placement.empty()) {''', '''        if (options.routing_mode == "pivot" && options.hash_placement) {
          pr = pivot_put_targets(
              options, vector_bl, {pivot_model->placement[row]},
              &locator_cache, &targets);
        } else if (options.routing_mode == "pivot") {
          const auto ranked = pivot_rank_pgs(
              base.rows[row].data(), pivot_model->dim, *pivot_model,
              options.pivot_probe_budget);
          std::vector<uint32_t> write_pgs(
              ranked.begin(),
              ranked.begin() + std::min<size_t>(options.d, ranked.size()));
          if (!pivot_model->placement.empty()) {''')

# the two fanout query routing sites
for qi, probes in (('query_index', 'qs.probes'), ('bq.query_index', 'bq.probes')):
    rep(f'''        if (options.routing_mode == "pivot") {{
          const auto ranked = pivot_rank_pgs(
              queries.rows[{qi}].data(), pivot_model->dim,
              *pivot_model, options.pivot_probe_budget);
          std::vector<uint32_t> probe_pgs(
              ranked.begin(),
              ranked.begin() + std::min<size_t>(options.m, ranked.size()));
          generated_groups = probe_pgs.size();
          rr = pivot_query_probes(options, probe_pgs, &cache, &{probes});''',
        f'''        if (options.routing_mode == "pivot" && options.hash_placement) {{
          const auto probe_pgs = hash_probe_pgs(
              options.seed, {qi}, options.pg_num, options.m);
          generated_groups = probe_pgs.size();
          rr = pivot_query_probes(options, probe_pgs, &cache, &{probes});
        }} else if (options.routing_mode == "pivot") {{
          const auto ranked = pivot_rank_pgs(
              queries.rows[{qi}].data(), pivot_model->dim,
              *pivot_model, options.pivot_probe_budget);
          std::vector<uint32_t> probe_pgs(
              ranked.begin(),
              ranked.begin() + std::min<size_t>(options.m, ranked.size()));
          generated_groups = probe_pgs.size();
          rr = pivot_query_probes(options, probe_pgs, &cache, &{probes});''')

# base read: query runs in hash mode need only the dimension
rep('''  auto base = read_fvecs(options.base_path,
                         cached_pivot_query ? 1 : options.base_limit, &error);''',
    '''  const bool hash_query = options.routing_mode == "pivot" &&
      options.hash_placement && options.operation != "load" &&
      options.operation != "both";
  auto base = read_fvecs(options.base_path,
                         (cached_pivot_query || hash_query) ? 1 : options.base_limit,
                         &error);''')

# model: hash mode builds a zero model + the hash placement instead
rep('''  if (options.routing_mode == "pivot") {
    const auto model_begin = clock_type::now();
    if (!options.pivot_model_file.empty()) {''', '''  if (options.routing_mode == "pivot" && options.hash_placement) {
    if ((options.pg_num & (options.pg_num - 1)) != 0 || options.d != 1 ||
        !options.pivot_model_file.empty() || options.sim_place_cap_eps > 0.0) {
      std::cerr << "error: --hash-placement needs a power-of-two --pg-num, --d 1,"
                   " and no --pivot-model-file / --pivot-place-cap-eps\\n";
      return 2;
    }
    pivot_model.emplace();
    pivot_model->dim = static_cast<int>(base->dimension);
    pivot_model->reps.assign(options.pg_num, std::vector<float>(base->dimension, 0.0f));
    if (options.operation == "load" || options.operation == "both") {
      pivot_model->placement.resize(base->rows.size());
      std::vector<uint64_t> fill(options.pg_num, 0);
      uint64_t h = 1469598103934665603ULL;
      for (size_t row = 0; row < base->rows.size(); ++row) {
        const uint32_t pg = hash_place_pg(row, options.pg_num);
        pivot_model->placement[row] = pg;
        ++fill[pg];
        h = (h ^ pg) * 1099511628211ULL;
      }
      std::cout << "HASH_PLACEMENT max_pg=" << *std::max_element(fill.begin(), fill.end())
                << " min_pg=" << *std::min_element(fill.begin(), fill.end())
                << " digest=" << std::hex << std::setw(16) << std::setfill('0') << h
                << std::dec << std::setfill(' ') << '\\n';
    }
  } else if (options.routing_mode == "pivot") {
    const auto model_begin = clock_type::now();
    if (!options.pivot_model_file.empty()) {''')

open(p, 'w').write(s)
print("patched")
