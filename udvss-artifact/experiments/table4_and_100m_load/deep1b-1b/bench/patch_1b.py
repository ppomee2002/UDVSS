#!/usr/bin/env python3
"""Deep1B 1B support for ceph-vector-bench-native (applied on top of the
crush-exp2 source = 948d1b4a949 + pivot-model-file + hash-placement).

1. --base may be a big-ann .fbin (u32 n, u32 dim, n*dim f32) as well as .fvecs;
   --base-offset R reads rows from R on (with --base-limit), and a load stores
   row r as global row R + r (key, entry_id, placement index).
2. --operation place: no cluster. Builds (or reads) the pivot model from the
   stride sample read straight from the file -- the same rows build_pivot_model()
   would pick from the whole base -- then the capped placement of every row,
   streaming the file in --place-chunk rows, keeping only --place-topn
   candidates per row; writes --placement-file (u64 magic, n, pg_num, n*u32).
3. --placement-file on a load: every row goes to its PG from that file.
4. load no longer ranks all representatives for a row whose PG is already
   fixed by a placement (the ranking was computed and then overwritten).
5. pivot_topt(): the nearest-T search used by capped_assign(), done 16
   representatives at a time. Each lane sums the dimensions in the same order
   as l2_dist_sq(), so (built with -ffp-contract=off) the distances and the
   (dist, pg) order are bit-identical to the scalar code; checked against the
   10M PG4096 model / placement digests.
"""
import sys
p = sys.argv[1]
s = open(p).read()


def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:100], s.count(a))
    s = s.replace(a, b)


# ---------------------------------------------------------------- options
rep('''  bool hash_placement = false;
''', '''  bool hash_placement = false;
  // Deep1B 1B: rows [base_offset, base_offset + base_limit) of --base; a load
  // stores row r as global row base_offset + r.
  uint64_t base_offset = 0;
  // --operation place writes it; a load reads every row's PG from it.
  std::string placement_file;
  // --operation place: candidates kept per row (capped_assign keeps 64) and
  // rows read per pass over the file.
  uint32_t place_topn = 64;
  uint64_t place_chunk = 20000000;
''')
rep('OPT_HASH_PLACEMENT,',
    'OPT_HASH_PLACEMENT, OPT_BASE_OFFSET, OPT_PLACEMENT_FILE, OPT_PLACE_TOPN, OPT_PLACE_CHUNK,')
rep('''    {"hash-placement", no_argument, nullptr, OPT_HASH_PLACEMENT},
''', '''    {"hash-placement", no_argument, nullptr, OPT_HASH_PLACEMENT},
    {"base-offset", required_argument, nullptr, OPT_BASE_OFFSET},
    {"placement-file", required_argument, nullptr, OPT_PLACEMENT_FILE},
    {"place-topn", required_argument, nullptr, OPT_PLACE_TOPN},
    {"place-chunk", required_argument, nullptr, OPT_PLACE_CHUNK},
''')
rep('''    case OPT_HASH_PLACEMENT: options->hash_placement = true; break;
''', '''    case OPT_HASH_PLACEMENT: options->hash_placement = true; break;
    case OPT_BASE_OFFSET: PARSE_OPT(base_offset); break;
    case OPT_PLACEMENT_FILE: options->placement_file = optarg; break;
    case OPT_PLACE_TOPN: PARSE_OPT(place_topn); break;
    case OPT_PLACE_CHUNK: PARSE_OPT(place_chunk); break;
''')
rep('''      << "  --hash-placement: pivot mode with CRUSH-style placement instead of\\n"
''', '''      << "  --base-offset R: read --base from row R (with --base-limit N);\\n"
      << "    a load stores row r as global row R + r. --base may be .fbin.\\n"
      << "  --operation place: no cluster; build/read the pivot model and write\\n"
      << "    the capped placement of every --base row to --placement-file\\n"
      << "    (--place-topn candidates per row, --place-chunk rows per pass).\\n"
      << "  --placement-file PATH: load takes every row's PG from PATH.\\n"
      << "  --hash-placement: pivot mode with CRUSH-style placement instead of\\n"
''')
# validation: place needs neither a pool nor queries
rep('''      (options->pool.empty() && options->operation != "route-sim")) {''',
    '''      (options->pool.empty() && options->operation != "route-sim" &&
       options->operation != "place")) {''')
rep('''      options->operation != "route-sim") {
    return false;
  }
  if (options->operation == "fanout-query" &&''', '''      options->operation != "route-sim" && options->operation != "place") {
    return false;
  }
  if (options->operation == "place" &&
      (options->routing_mode != "pivot" || options->placement_file.empty() ||
       options->pivot_model_file.empty() || options->d != 1 ||
       options->sim_place_cap_eps <= 0.0 || options->place_topn == 0 ||
       options->place_chunk == 0)) {
    std::cerr << "--operation place needs --routing-mode pivot, --placement-file,"
                 " --pivot-model-file, --d 1 and --pivot-place-cap-eps > 0\\n";
    return false;
  }
  if (options->operation == "fanout-query" &&''')
rep('''  if (options->operation != "load" && options->query_path.empty()) {''',
    '''  if (options->operation != "load" && options->operation != "place" &&
      options->query_path.empty()) {''')

# ---------------------------------------------------------------- base files
rep('''std::optional<float_dataset_t> read_fvecs(
    const std::string& path, size_t limit, std::string *error)
{''', '''// Fixed-row-size view of a .fvecs (u32 dim + dim f32 per row) or a big-ann
// .fbin (u32 n, u32 dim header, then n*dim f32) file, for reading any row
// range without holding the file (1B rows = 384 GB).
struct vec_file_t {
  std::string path;
  bool fbin = false;
  uint32_t dim = 0;
  uint64_t rows = 0;
  uint64_t header = 0;    // bytes before row 0
  uint64_t row_bytes = 0; // bytes per row on disk
  int fd = -1;
  ~vec_file_t() { if (fd >= 0) ::close(fd); }
};

bool open_vec_file(const std::string& path, vec_file_t *f, std::string *error)
{
  f->path = path;
  f->fbin = path.size() > 5 && path.compare(path.size() - 5, 5, ".fbin") == 0;
  f->fd = ::open(path.c_str(), O_RDONLY);
  if (f->fd < 0) { *error = "cannot open " + path; return false; }
  struct stat st;
  if (::fstat(f->fd, &st) != 0) { *error = "cannot stat " + path; return false; }
  const uint64_t size = static_cast<uint64_t>(st.st_size);
  uint32_t hdr[2] = {0, 0};
  if (::pread(f->fd, hdr, f->fbin ? 8 : 4, 0) != (f->fbin ? 8 : 4)) {
    *error = "truncated header in " + path; return false;
  }
  f->dim = f->fbin ? hdr[1] : hdr[0];
  if (f->dim == 0 || f->dim > LIBRADOS_VECTOR_MAX_DIMENSION) {
    *error = "invalid dimension in " + path; return false;
  }
  f->header = f->fbin ? 8 : 0;
  f->row_bytes = (f->fbin ? 0 : 4) + uint64_t(f->dim) * sizeof(float);
  f->rows = (size - f->header) / f->row_bytes;
  if (f->fbin && f->rows < hdr[0]) {
    *error = "truncated fbin " + path; return false;
  }
  if (f->fbin) f->rows = hdr[0];
  return true;
}

// Rows [offset, offset + count) into out (dim floats each, contiguous).
bool read_vec_rows(const vec_file_t& f, uint64_t offset, uint64_t count,
                   float *out, std::string *error)
{
  constexpr uint64_t kBlock = 1 << 16;  // rows per pread
  std::vector<char> buf;
  for (uint64_t done = 0; done < count; ) {
    const uint64_t n = std::min(kBlock, count - done);
    const uint64_t bytes = n * f.row_bytes;
    buf.resize(bytes);
    uint64_t got = 0;
    const uint64_t pos = f.header + (offset + done) * f.row_bytes;
    while (got < bytes) {
      const ssize_t r = ::pread(f.fd, buf.data() + got, bytes - got, pos + got);
      if (r <= 0) { *error = "short read in " + f.path; return false; }
      got += static_cast<uint64_t>(r);
    }
    for (uint64_t i = 0; i < n; ++i) {
      const char *src = buf.data() + i * f.row_bytes + (f.fbin ? 0 : 4);
      std::memcpy(out + (done + i) * f.dim, src, f.dim * sizeof(float));
    }
    done += n;
  }
  return true;
}

// float_dataset_t of rows [offset, offset + limit) (limit 0 = to the end).
std::optional<float_dataset_t> read_base_rows(
    const std::string& path, uint64_t offset, uint64_t limit, std::string *error)
{
  vec_file_t f;
  if (!open_vec_file(path, &f, error)) return std::nullopt;
  if (offset >= f.rows) { *error = "--base-offset past the end of " + path; return std::nullopt; }
  const uint64_t count = limit == 0 ? f.rows - offset : std::min(limit, f.rows - offset);
  float_dataset_t data;
  data.dimension = f.dim;
  data.rows.assign(count, std::vector<float>(f.dim));
  constexpr uint64_t kPart = 1 << 20;
  std::vector<float> part;
  for (uint64_t done = 0; done < count; done += kPart) {
    const uint64_t n = std::min(kPart, count - done);
    part.resize(n * f.dim);
    if (!read_vec_rows(f, offset + done, n, part.data(), error)) return std::nullopt;
    for (uint64_t i = 0; i < n; ++i) {
      std::memcpy(data.rows[done + i].data(), part.data() + i * f.dim, f.dim * sizeof(float));
    }
  }
  return data;
}

std::optional<float_dataset_t> read_fvecs(
    const std::string& path, size_t limit, std::string *error)
{''')
rep('#include <getopt.h>\n', '#include <getopt.h>\n#include <fcntl.h>\n#include <sys/stat.h>\n#include <unistd.h>\n')

# ---------------------------------------------------------------- fast nearest-T
rep('''// Capacity-constrained assignment for balanced Lloyd and capped placement.''',
'''// Nearest-T representatives of a set of rows by squared L2, ordered by
// (dist, pg) -- exactly what partial_sort over every (dist, pg) pair gives.
// Representatives are laid out 16 to a block, dimension-major, so each SIMD
// lane accumulates one representative's distance over the dimensions in the
// same order as l2_dist_sq(): with -ffp-contract=off the distances are
// bit-identical to the scalar loop.
struct rep_blocks_t {
  size_t k = 0, dim = 0, blocks = 0;
  std::vector<float> t;  // t[(b * dim + d) * 16 + j] = reps[16 b + j][d]
};

rep_blocks_t make_rep_blocks(const PivotModel& model)
{
  rep_blocks_t rb;
  rb.k = model.reps.size(); rb.dim = model.dim; rb.blocks = (rb.k + 15) / 16;
  rb.t.assign(rb.blocks * rb.dim * 16, 0.0f);
  for (size_t p = 0; p < rb.k; ++p) {
    for (size_t d = 0; d < rb.dim; ++d) {
      rb.t[((p / 16) * rb.dim + d) * 16 + p % 16] = model.reps[p][d];
    }
  }
  return rb;
}

// rows: nrows row pointers; out_pg / out_d: nrows * topn (row-major).
// Tiles of kRows rows x kGroup blocks (64 representatives, 24 KiB at dim 96)
// so the representatives come from L1; one 16-lane accumulator per
// (row, block). Lane j adds (x[d] - c_j[d])^2 for d = 0..dim-1 in order --
// the scalar loop's exact sequence of IEEE operations.
typedef float v16f __attribute__((vector_size(64)));

inline void topt_insert(float dist, uint32_t p, size_t topn, size_t *n,
                        uint32_t *pg, float *dd)
{
  // p only grows, so an equal distance never displaces a kept entry
  if (*n == topn && !(dist < dd[topn - 1])) return;
  size_t pos = *n < topn ? *n : topn - 1;
  while (pos > 0 && dist < dd[pos - 1]) {
    dd[pos] = dd[pos - 1]; pg[pos] = pg[pos - 1]; --pos;
  }
  dd[pos] = dist; pg[pos] = p;
  if (*n < topn) ++*n;
}

void pivot_topt(const float *const *rows, size_t nrows, const rep_blocks_t& rb,
                size_t topn, uint32_t *out_pg, float *out_d)
{
  constexpr size_t kRows = 8, kGroup = 4;
  const size_t dim = rb.dim;
  for (size_t r0 = 0; r0 < nrows; r0 += kRows) {
    const size_t nr = std::min(kRows, nrows - r0);
    size_t fill[kRows] = {};
    for (size_t b0 = 0; b0 < rb.blocks; b0 += kGroup) {
      const size_t ng = std::min(kGroup, rb.blocks - b0);
      for (size_t r = 0; r < nr; ++r) {
        const float *v = rows[r0 + r];
        v16f acc[kGroup];
        for (size_t g = 0; g < kGroup; ++g) acc[g] = v16f{} ;
        const float *blk = rb.t.data() + b0 * dim * 16;
        if (ng == kGroup) {
          for (size_t d = 0; d < dim; ++d) {
            const v16f x = v16f{} + v[d];
            for (size_t g = 0; g < kGroup; ++g) {
              v16f c;
              std::memcpy(&c, blk + (g * dim + d) * 16, sizeof(c));
              const v16f diff = x - c;
              acc[g] += diff * diff;
            }
          }
        } else {
          for (size_t g = 0; g < ng; ++g) {
            for (size_t d = 0; d < dim; ++d) {
              v16f c;
              std::memcpy(&c, blk + (g * dim + d) * 16, sizeof(c));
              const v16f diff = (v16f{} + v[d]) - c;
              acc[g] += diff * diff;
            }
          }
        }
        uint32_t *pg = out_pg + (r0 + r) * topn;
        float *dd = out_d + (r0 + r) * topn;
        for (size_t g = 0; g < ng; ++g) {
          const size_t b = b0 + g;
          if (fill[r] == topn) {
            const v16f worst = v16f{} + dd[topn - 1];
            const auto lt = acc[g] < worst;
            bool any = false;
            for (int j = 0; j < 16; ++j) any |= lt[j] != 0;
            if (!any) continue;
          }
          for (int j = 0; j < 16; ++j) {
            const uint32_t p = static_cast<uint32_t>(b * 16 + j);
            if (p >= rb.k) break;
            topt_insert(acc[g][j], p, topn, &fill[r], pg, dd);
          }
        }
      }
    }
  }
}

// Capacity-constrained assignment for balanced Lloyd and capped placement.''')
rep('''  std::vector<uint32_t> cand_pg(n * topn);
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
  });''', '''  std::vector<uint32_t> cand_pg(n * topn);
  std::vector<float> cand_d(n * topn);
  const rep_blocks_t rb = make_rep_blocks(model);
  parallel_for(n, [&](size_t b, size_t e) {
    std::vector<const float *> ptr(e - b);
    for (size_t i = b; i < e; ++i) ptr[i - b] = row(i);
    pivot_topt(ptr.data(), ptr.size(), rb, topn,
               cand_pg.data() + b * topn, cand_d.data() + b * topn);
  });''')

# streaming placement for --operation place
rep('''void balanced_assign(const std::vector<const std::vector<float> *>& samples,''',
'''// capped_assign() over a file too big to hold: pass 1 streams --place-chunk
// rows at a time and keeps each row's nearest `topn` (pg) plus its nearest
// distance; pass 2 is capped_assign()'s greedy, unchanged. With topn = 64 the
// result is capped_assign()'s exactly. rank_hist[t] counts rows that got
// their t-th candidate; rank_hist[topn] the rows whose candidates were all
// full (least-filled fallback).
// IdT = uint16_t when pg_num <= 65536 (1B x 16 candidates = 32 GB, not 64).
template <typename IdT>
bool capped_assign_file_t(const vec_file_t& f, const PivotModel& model, double eps,
                          size_t topn, uint64_t chunk, std::vector<uint32_t> *placement,
                          std::vector<uint64_t> *rank_hist, std::string *error)
{
  const size_t k = model.reps.size();
  topn = std::min<size_t>(topn, k);
  const uint64_t n = f.rows;
  const size_t cap = static_cast<size_t>(
      std::ceil((1.0 + eps) * static_cast<double>(n) / static_cast<double>(k)));
  std::vector<IdT> cand_pg(n * topn);
  std::vector<float> d0(n);
  const rep_blocks_t rb = make_rep_blocks(model);
  std::vector<float> buf;
  std::vector<float> dtmp;
  std::vector<uint32_t> ptmp;
  const auto t0 = clock_type::now();
  for (uint64_t o = 0; o < n; o += chunk) {
    const uint64_t c = std::min(chunk, n - o);
    buf.resize(c * f.dim);
    if (!read_vec_rows(f, o, c, buf.data(), error)) return false;
    dtmp.resize(c * topn);
    ptmp.resize(c * topn);
    parallel_for(c, [&](size_t b, size_t e) {
      std::vector<const float *> ptr(e - b);
      for (size_t i = b; i < e; ++i) ptr[i - b] = buf.data() + i * f.dim;
      pivot_topt(ptr.data(), ptr.size(), rb, topn,
                 ptmp.data() + b * topn, dtmp.data() + b * topn);
    });
    for (uint64_t i = 0; i < c; ++i) d0[o + i] = dtmp[i * topn];
    for (uint64_t i = 0; i < c * topn; ++i) cand_pg[o * topn + i] = static_cast<IdT>(ptmp[i]);
    std::cout << "PLACE_PASS1 rows=" << (o + c) << "/" << n << " seconds="
              << std::fixed << std::setprecision(1)
              << std::chrono::duration<double>(clock_type::now() - t0).count()
              << std::setprecision(6) << std::defaultfloat << std::endl;
  }
  std::vector<uint32_t> order(n);
  std::iota(order.begin(), order.end(), 0u);
  std::sort(order.begin(), order.end(), [&](uint32_t a, uint32_t b) {
    if (d0[a] != d0[b]) return d0[a] < d0[b];
    return a < b;
  });
  std::vector<uint32_t>().swap(*placement);
  placement->assign(n, 0);
  rank_hist->assign(topn + 1, 0);
  std::vector<size_t> fill(k, 0);
  for (const uint32_t i : order) {
    const IdT *cp = cand_pg.data() + uint64_t(i) * topn;
    size_t pick = topn;
    for (size_t t = 0; t < topn; ++t) {
      if (fill[cp[t]] < cap) { pick = t; break; }
    }
    ++(*rank_hist)[pick];
    if (pick == topn) {
      pick = 0;
      for (size_t t = 1; t < topn; ++t) {
        if (fill[cp[t]] < fill[cp[pick]]) pick = t;
      }
    }
    ++fill[cp[pick]];
    (*placement)[i] = cp[pick];
  }
  return true;
}

bool capped_assign_file(const vec_file_t& f, const PivotModel& model, double eps,
                        size_t topn, uint64_t chunk, std::vector<uint32_t> *placement,
                        std::vector<uint64_t> *rank_hist, std::string *error)
{
  if (model.reps.size() <= 65536) {
    return capped_assign_file_t<uint16_t>(f, model, eps, topn, chunk, placement, rank_hist, error);
  }
  return capped_assign_file_t<uint32_t>(f, model, eps, topn, chunk, placement, rank_hist, error);
}

constexpr uint64_t PLACEMENT_FILE_MAGIC = 0x31544e434c505654ULL; // "TVPLCNT1"

bool save_placement(const std::string& path, const std::vector<uint32_t>& pl, uint32_t pg_num)
{
  const std::string tmp = path + ".tmp";
  std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
  if (!out) return false;
  const uint64_t hdr[3] = {PLACEMENT_FILE_MAGIC, pl.size(), pg_num};
  out.write(reinterpret_cast<const char *>(hdr), sizeof(hdr));
  out.write(reinterpret_cast<const char *>(pl.data()), pl.size() * sizeof(uint32_t));
  out.close();
  return out && std::rename(tmp.c_str(), path.c_str()) == 0;
}

bool read_placement(const std::string& path, uint32_t pg_num,
                    std::vector<uint32_t> *pl, std::string *error)
{
  std::ifstream in(path, std::ios::binary);
  uint64_t hdr[3] = {};
  in.read(reinterpret_cast<char *>(hdr), sizeof(hdr));
  if (!in || hdr[0] != PLACEMENT_FILE_MAGIC || hdr[2] != pg_num) {
    *error = "bad placement file (or pg_num mismatch) " + path; return false;
  }
  pl->resize(hdr[1]);
  in.read(reinterpret_cast<char *>(pl->data()), pl->size() * sizeof(uint32_t));
  if (!in) { *error = "truncated placement file " + path; return false; }
  return true;
}

uint64_t placement_digest(const std::vector<uint32_t>& pl)
{
  uint64_t h = 1469598103934665603ULL;
  for (const uint32_t pg : pl) h = (h ^ pg) * 1099511628211ULL;
  return h;
}

void balanced_assign(const std::vector<const std::vector<float> *>& samples,''')

# ---------------------------------------------------------------- load loop
rep('''        const auto vector_bl = vector_buffer_typed(base.rows[row], config.data_type,
                                          options.quantize_scale);
        std::vector<pg_lsh::put_target_t> targets;
        int pr;
        if (options.routing_mode == "pivot" && options.hash_placement) {
          pr = pivot_put_targets(
              options, vector_bl, {pivot_model->placement[row]},
              &locator_cache, &targets);
        } else if (options.routing_mode == "pivot") {''',
'''        const uint64_t grow = row + options.base_offset;  // global row
        const auto vector_bl = vector_buffer_typed(base.rows[row], config.data_type,
                                          options.quantize_scale);
        std::vector<pg_lsh::put_target_t> targets;
        int pr;
        if (options.routing_mode == "pivot" && !pivot_model->placement.empty()) {
          // the PG is fixed (hash, capped or --placement-file placement):
          // ranking every representative would only be thrown away
          pr = pivot_put_targets(
              options, vector_bl, {pivot_model->placement[grow]},
              &locator_cache, &targets);
        } else if (options.routing_mode == "pivot") {''')
rep('''          req.key = vector_key(options, row);''', '''          req.key = vector_key(options, grow);''')

# ---------------------------------------------------------------- main
rep('''  auto base = read_fvecs(options.base_path,
                         (cached_pivot_query || hash_query) ? 1 : options.base_limit,
                         &error);''', '''  if (options.operation == "place") {
    vec_file_t f;
    if (!open_vec_file(options.base_path, &f, &error)) {
      std::cerr << "error: " << error << '\\n'; return 2;
    }
    // the stride sample build_pivot_model() would take from the whole file
    const uint64_t sample_limit = std::min<uint64_t>(
        f.rows, std::max<uint64_t>(1, options.pivot_build_sample));
    const uint64_t stride = std::max<uint64_t>(1, f.rows / sample_limit);
    std::optional<PivotModel> model = read_pivot_model(options.pivot_model_file, &error);
    if (!model && !error.empty()) { std::cerr << "error: " << error << '\\n'; return 2; }
    const auto mb = clock_type::now();
    if (model) {
      std::cout << "PIVOT_MODEL_FILE read " << options.pivot_model_file << '\\n';
    } else {
      float_dataset_t sample;
      sample.dimension = f.dim;
      std::vector<float> row(f.dim);
      for (uint64_t i = 0; i < f.rows && sample.rows.size() < sample_limit; i += stride) {
        if (!read_vec_rows(f, i, 1, row.data(), &error)) {
          std::cerr << "error: " << error << '\\n'; return 2;
        }
        sample.rows.push_back(row);
      }
      model = build_pivot_model(sample, options.pg_num, sample.rows.size(),
                                options.pivot_refine_iters, options.pivot_spherical,
                                options.pivot_balance_eps);
      if (!save_pivot_model(options.pivot_model_file, *model)) {
        std::cerr << "error: cannot write " << options.pivot_model_file << '\\n'; return 2;
      }
      std::cout << "PIVOT_MODEL_FILE wrote " << options.pivot_model_file << '\\n';
    }
    std::cout << "PIVOT_MODEL mode=pivot seed=" << options.seed
              << " sample_count=" << model->sample_count
              << " representative_count=" << model->reps.size()
              << " dim=" << model->dim
              << " refine_iters=" << options.pivot_refine_iters
              << " spherical=" << (options.pivot_spherical ? 1 : 0)
              << " balance_eps=" << options.pivot_balance_eps
              << " build_seconds=" << std::fixed << std::setprecision(3)
              << std::chrono::duration<double>(clock_type::now() - mb).count()
              << std::setprecision(6)
              << " digest=" << std::hex << std::setw(16) << std::setfill('0')
              << pivot_model_digest(model->reps)
              << std::dec << std::setfill(' ') << std::endl;
    if (model->reps.size() != options.pg_num || uint64_t(model->dim) != f.dim) {
      std::cerr << "error: pivot model does not match --pg-num / --base\\n"; return 1;
    }
    const auto pb = clock_type::now();
    std::vector<uint32_t> pl;
    std::vector<uint64_t> hist;
    if (!capped_assign_file(f, *model, options.sim_place_cap_eps, options.place_topn,
                            options.place_chunk, &pl, &hist, &error)) {
      std::cerr << "error: " << error << '\\n'; return 2;
    }
    std::vector<uint64_t> fill(options.pg_num, 0);
    for (const uint32_t pg : pl) ++fill[pg];
    std::cout << "PIVOT_PLACEMENT cap_eps=" << options.sim_place_cap_eps
              << " topn=" << options.place_topn
              << " max_pg=" << *std::max_element(fill.begin(), fill.end())
              << " min_pg=" << *std::min_element(fill.begin(), fill.end())
              << " seconds=" << std::fixed << std::setprecision(3)
              << std::chrono::duration<double>(clock_type::now() - pb).count()
              << std::setprecision(6)
              << " digest=" << std::hex << std::setw(16) << std::setfill('0')
              << placement_digest(pl) << std::dec << std::setfill(' ') << '\\n';
    std::cout << "PLACE_RANK_HIST";
    for (size_t t = 0; t < hist.size(); ++t) {
      if (hist[t]) std::cout << ' ' << (t == hist.size() - 1 ? std::string("fallback") : std::to_string(t)) << '=' << hist[t];
    }
    std::cout << '\\n';
    if (!save_placement(options.placement_file, pl, options.pg_num)) {
      std::cerr << "error: cannot write " << options.placement_file << '\\n'; return 2;
    }
    std::cout << "PLACEMENT_FILE wrote " << options.placement_file << " rows=" << pl.size() << '\\n';
    return 0;
  }
  auto base = read_base_rows(options.base_path,
                             (cached_pivot_query || hash_query) ? 0 : options.base_offset,
                             (cached_pivot_query || hash_query) ? 1 : options.base_limit,
                             &error);''')

# load with --placement-file: placement from the file, no capped pass
rep('''  if (pivot_model && options.sim_place_cap_eps > 0.0 &&
      (options.operation == "load" || options.operation == "both")) {''',
'''  if (pivot_model && !options.placement_file.empty() &&
      (options.operation == "load" || options.operation == "both")) {
    if (!read_placement(options.placement_file, options.pg_num,
                        &pivot_model->placement, &error)) {
      std::cerr << "error: " << error << '\\n'; return 2;
    }
    if (options.base_offset + base->rows.size() > pivot_model->placement.size()) {
      std::cerr << "error: placement file has " << pivot_model->placement.size()
                << " rows, load needs up to " << options.base_offset + base->rows.size() << '\\n';
      return 2;
    }
    std::cout << "PLACEMENT_FILE read " << options.placement_file
              << " rows=" << pivot_model->placement.size()
              << " digest=" << std::hex << std::setw(16) << std::setfill('0')
              << placement_digest(pivot_model->placement)
              << std::dec << std::setfill(' ') << '\\n';
  } else if (pivot_model && options.sim_place_cap_eps > 0.0 &&
      (options.operation == "load" || options.operation == "both")) {
    if (options.base_offset != 0) {
      std::cerr << "error: --base-offset needs --placement-file\\n"; return 2;
    }''')

# 6. Lloyd refinement: with balance_eps > 0 the plain nearest-representative
#    pass is overwritten in full by balanced_assign() right after it, so skip it
#    (scalar, ~4 h of the 1B build); the model is unchanged bit for bit.
rep('''    parallel_for(samples.size(), [&](size_t b, size_t e) {
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
    if (balance_eps > 0.0) {''', '''    if (balance_eps <= 0.0) parallel_for(samples.size(), [&](size_t b, size_t e) {
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
    if (balance_eps > 0.0) {''')

open(p, 'w').write(s)
print("patched")
