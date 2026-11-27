#!/usr/bin/env python3
"""Ceph (crimson + seastore VectorNode, pivot routing) arm of the FP32 vs SQ8
memory experiment.  One process per representation (ARM=fp32|int8), each with
its own NEW vstart cluster (scripts/00_cluster.sh) and its own OSD core set.

Per N (fresh pool each time; the bench tool cannot grow an index in place):
  load   : OSD at LOAD_CAP, load --base-limit N            -> peak during load
  for cap in CAPS, cache in (default, half):
    restart OSD with --crimson_memory cap [--seastore_cachepin_size_pershard]
    stable : 30 s after the OSD is up and all PGs active      -> after-boot memory
    query  : fanout-query sized to ~30 s (after a short probe) -> peak, QPS, recall
Failure = OSD not up / died / malloc_failed > 0 / query failures.

Memory is measured from the host: /proc/<osd>/status every 0.5 s (no cgroup:
the OSD runs on the shared host; the budget is seastar's own --memory cap),
plus `ceph tell osd.0 dump_metrics` (seastar allocator + seastore cache).
"""
import json, math, os, re, signal, subprocess, sys, threading, time
import numpy as np

ARM = os.environ["ARM"]
ROOT = "/home/dev_path/experiments/memfootprint-sq8"
EXP = f"{ROOT}/ceph"
BUILD = "/home/dev_path/ceph-vectornode-seastore-batch/build"   # ll3 pivots, same build as workload-bench
BIN = f"{BUILD}/bin"
VARIANT = os.environ.get("VARIANT", "base")
if VARIANT == "prof":
    # reference configuration: smp 2, RANDOM_BLOCK_SSD + cbjournal 1.5G, cachepin 1.5G, crimson_memory 4G
    VDEST = f"/home/dev_path/vstart/memfp2-{ARM}"
    CPUSET = "0-1" if ARM == "fp32" else "2-3"
    CPUSET = os.environ.get("OSD_CPUSET_OVERRIDE", CPUSET)   # 2026-10-16: 0-7 busy with the mem48 OSD
    EXTRA_OSD = ["--seastore_main_device_type", "RANDOM_BLOCK_SSD",
                 "--seastore_cbjournal_size", os.environ.get("CBJ", "1610612736")]
else:
    VDEST = f"/home/dev_path/vstart/memfp-{ARM}"
    CPUSET = "0-7" if ARM == "fp32" else "8-15"
    EXTRA_OSD = []
CONF = f"{VDEST}/ceph.conf"
CACHE_FIXED = int(os.environ.get("CACHE_FIXED", "0"))
NO_RESTART = os.environ.get("NO_RESTART", "0") == "1"   # bytes per reactor; 0 = use the cache plan
QUANT = "float32" if ARM == "fp32" else "int8"
CLIENT = "16-23,40-47"
DATA = "/home/dev_path/ceph/cohere1m"
IDX = ("--routing-mode pivot --pg-num 1024 --k 10 --l 4 --seed 1315423911 --d 1 "
       "--pivot-build-sample 16384 --pivot-refine-iters 3 --pivot-probe-budget 96").split()
STEPS = [int(x) for x in os.environ.get("STEPS", "250000,500000,1000000,2000000").split(",")]
CAPS = os.environ.get("CAPS", "16G,8G,4G,2G").split(",")
HALF_CAPS = os.environ.get("HALF_CAPS", "8G,4G,2G").split(",")
FALLBACK = os.environ.get("FALLBACK", "1") == "1"
RESUME_N = int(os.environ.get("RESUME_N", "0"))
RESUME_PLAN = [tuple(x.split(":")) for x in os.environ.get("RESUME_PLAN", "").split(",") if x]
LOAD_CAP = os.environ.get("LOAD_CAP", "24G")
M = 16
NQ = 200
DIM = 768
G = 1024 ** 3
ENV = dict(os.environ, LD_LIBRARY_PATH=f"{BUILD}/lib", CEPH_CONF=CONF,
           CEPH_KEYRING=f"{VDEST}/keyring")
RDIR = f"{EXP}/results/{os.environ.get('RTAG', 'main')}/{ARM}"
os.makedirs(RDIR, exist_ok=True)
LOGF = open(f"{RDIR}/run.log", "a")
QUERY_LOCK = f"{EXP}/logs/.query.lock"

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def log(*a):
    s = f"[{now()}] [{ARM}] " + " ".join(str(x) for x in a)
    print(s, flush=True); LOGF.write(s + "\n"); LOGF.flush()

def sh(args, timeout=600, check=False):
    try:
        r = subprocess.run(args, env=ENV, cwd=BUILD, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        # a hung client (e.g. OSD in a bad_alloc loop dropped the connection) is a
        # measurement, not a harness crash (fp32 0.5M 4G default-cache, 2026-10-02)
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return subprocess.CompletedProcess(args, -9, out, f"TIMEOUT after {timeout}s")
    if check and r.returncode:
        raise RuntimeError(f"{args[:3]} rc={r.returncode}: {r.stderr[-500:]}")
    return r

def ceph(*a, timeout=120):
    return sh([f"{BIN}/ceph", "-c", CONF] + list(a), timeout=timeout)

# ---- OSD control -----------------------------------------------------------------
def osd_pids():
    out = subprocess.run(["pgrep", "-f", f"crimson-osd -i 0 -c {CONF}"], capture_output=True,
                         text=True).stdout.split()
    return [int(p) for p in out]

def safe_stop():
    """seastore's cleaner only reclaims below 15 % available and is not ready during
    mount; stopping with a shard near that line made the next mount abort with
    'out of space' (24G/64M-segment attempt).  Wait while GC is active."""
    if osd_pids():
        for _ in range(60):
            r = metrics().get("seg_avail_min", 1.0)
            if r >= 0.15:
                break
            log(f"  waiting for seastore GC before restart (min available ratio {r:.3f})")
            time.sleep(10)
    osd_stop()

def osd_stop():
    # vstart's ceph-run wrapper restarts the daemon on exit; remove it first (own cluster only)
    for p in subprocess.run(["pgrep", "-f", f"ceph-run .*crimson-osd -i 0 -c {CONF}"],
                            capture_output=True, text=True).stdout.split():
        os.kill(int(p), signal.SIGTERM)
    for p in osd_pids():
        os.kill(p, signal.SIGTERM)
    for _ in range(60):
        if not osd_pids():
            return
        time.sleep(2)
    for p in osd_pids():                       # own OSD stuck in shutdown
        log("  OSD did not exit in 120 s, SIGKILL", p)
        os.kill(p, signal.SIGKILL)
    time.sleep(3)

OSD_ARGS = {}
def osd_start(cap, cache_pershard=None, timeout=600):
    args = [f"{BIN}/crimson-osd", "-i", "0", "-c", CONF, "-f", "--crimson_memory", cap,
            "--crimson_cpu_set", CPUSET] + EXTRA_OSD
    if cache_pershard:
        args += ["--seastore_cachepin_size_pershard", str(cache_pershard)]
    OSD_ARGS.update(cap=cap, cache_pershard=cache_pershard or "default(2G)")
    out = open(f"{EXP}/logs/osd_{ARM}_stdout.log", "a")
    out.write(f"\n##### {now()} start {' '.join(args)}\n"); out.flush()
    subprocess.Popen(["bash", "-c", "ulimit -l unlimited; exec " + " ".join(args)],
                     env=ENV, cwd=BUILD, stdout=out, stderr=subprocess.STDOUT,
                     start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        if not osd_pids():
            return False, "osd_died_at_start"
        s = ceph("pg", "stat", timeout=30).stdout
        up = "1 up" in ceph("osd", "stat", timeout=30).stdout
        m = re.search(r"(\d+) pgs: (.*?);", s)
        if up and m and (m.group(2).strip() == f"{m.group(1)} active+clean" or int(m.group(1)) == 0):
            return True, f"up in {int(time.time()-t0)}s"
    return False, "not_up_timeout"

def bad_alloc_count():
    try:
        out = subprocess.run(["grep", "-ac", "bad_alloc", f"{VDEST}/out/osd.0.log"],
                             capture_output=True, text=True, timeout=120).stdout.strip()
        return int(out or 0)
    except Exception:
        return -1

def metrics():
    r = ceph("tell", "osd.0", "dump_metrics", timeout=60)
    try:
        j = json.loads(r.stdout)
    except Exception:
        return {}
    acc = {}
    for m in j.get("metrics", []):
        for k, v in m.items():
            if k == "segment_cleaner_available_ratio" and isinstance(v, dict):
                acc["seg_avail_min"] = min(acc.get("seg_avail_min", 1.0), v["value"])
            if k.startswith(("memory_", "cache_cached_extent", "cache_lru_size", "cache_lru_hit",
                             "cache_lru_miss", "cache_cache_hit", "cache_cache_access",
                             "cache_dirty_extent_bytes")) and isinstance(v, dict) and "value" in v:
                acc[k] = acc.get(k, 0) + v["value"]
    return acc

# ---- sampler ------------------------------------------------------------------------
class Sampler(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.fh = open(path, "a"); self.samples = []; self.lock = threading.Lock(); self.stopped = False
    def run(self):
        while not self.stopped:
            t = time.time(); d = {"t": t}
            pids = osd_pids()
            if pids:
                p = max(pids, key=lambda x: self._rss(x)[0])
                d["pid"] = p
                d["rss"], d["anon"], d["file"] = self._rss(p)
            for l in open("/proc/meminfo"):
                k, v = l.split(":")[0], l.split()[1]
                if k in ("MemTotal", "MemAvailable"):
                    d[k] = int(v) * 1024
            self.fh.write(json.dumps(d) + "\n"); self.fh.flush()
            with self.lock:
                self.samples.append(d)
            time.sleep(max(0, 0.5 - (time.time() - t)))
    @staticmethod
    def _rss(p):
        try:
            st = open(f"/proc/{p}/status").read()
            g = lambda k: int(re.search(rf"^{k}:\s+(\d+)", st, re.M).group(1)) * 1024
            return g("VmRSS"), g("RssAnon"), g("RssFile")
        except Exception:
            return 0, 0, 0
    def mark(self, s):
        self.fh.write(json.dumps({"t": time.time(), "mark": s}) + "\n"); self.fh.flush()
    def window(self, t0, t1):
        with self.lock:
            return [s for s in self.samples if t0 <= s["t"] <= t1 and "rss" in s]

def summ(w):
    if not w:
        return {}
    last = w[-1]
    return {"rss_gb": last["rss"] / G, "rss_anon_gb": last["anon"] / G, "rss_file_gb": last["file"] / G,
            "peak_rss_gb": max(s["rss"] for s in w) / G, "peak_rss_anon_gb": max(s["anon"] for s in w) / G,
            "node_used_gb": (last["MemTotal"] - last["MemAvailable"]) / G,
            "node_avail_gb": last["MemAvailable"] / G}

# ---- bench --------------------------------------------------------------------------
def base_args(n):
    if n <= 1_000_000:
        return ["--base", f"{DATA}/cohere_base.fvecs", "--base-limit", str(n)]
    if os.environ.get("AUG_FILE"):          # one large augmented file, read as a prefix
        return ["--base", os.environ["AUG_FILE"], "--base-limit", str(n)]
    return ["--base", f"{EXP}/data/aug_{n // 1_000_000}M.fvecs", "--base-limit", str(n)]

def bench(op, pool, n, extra, timeout):
    args = (["taskset", "-c", CLIENT, f"{BIN}/ceph-vector-bench-native", "--operation", op,
             "--pool", pool, "--index", f"{pool}_index"] + IDX + base_args(n) +
            ["--query", f"{DATA}/cohere_query.fvecs", "--groundtruth", f"{EXP}/data/gt_{n}.ivecs",
             "--query-limit", str(NQ), "--top-k", "10", "--quantize", QUANT] + extra)
    r = sh(args, timeout=timeout)
    with open(f"{RDIR}/bench_raw.log", "a") as fh:
        fh.write(f"### {now()} {' '.join(args)}\n{r.stdout[-6000:]}\n{r.stderr[-3000:]}\n")
    return r

def kv(line):
    return {k: v for k, v in (t.split("=", 1) for t in line.split() if "=" in t)}

def query(pool, n):
    common = ["--m", str(M), "--query-concurrency", "40", "--fanout-mode", "parallel",
              "--query-batch-size", "32"]
    r = bench("fanout-query", pool, n, common + ["--rounds", "2", "--warmup-rounds", "1"], 900)
    lines = [l for l in r.stdout.splitlines() if l.startswith("FANOUT_QUERY")]
    if not lines:
        return None, f"probe failed rc={r.returncode}: {(r.stderr or r.stdout)[-300:]}"
    probe = kv(lines[-1])
    qps = float(probe.get("logical_qps", 0) or 0)
    rounds = int(min(2000, max(4, math.ceil(qps * 30 / NQ))))
    t0 = time.time()
    r = bench("fanout-query", pool, n, common + ["--rounds", str(rounds), "--warmup-rounds", "0"], 900)
    lines = [l for l in r.stdout.splitlines() if l.startswith("FANOUT_QUERY")]
    if not lines:
        return None, f"query failed rc={r.returncode}: {(r.stderr or r.stdout)[-300:]}"
    q = kv(lines[-1]); q["_t0"], q["_t1"], q["_rounds"] = t0, time.time(), rounds
    return q, None

# ---- rows ---------------------------------------------------------------------------
COLS = ["system", "mode", "cap", "cache_pershard", "phase", "N", "dim", "raw_vec_gb_fp32",
        "raw_vec_gb_repr", "pool_stored_gb", "cache_extent_gb", "cache_lru_gb",
        "seastar_alloc_gb", "seastar_total_gb", "malloc_failed",
        "rss_gb", "rss_anon_gb", "rss_file_gb", "peak_rss_gb", "peak_rss_anon_gb",
        "node_used_gb", "node_avail_gb", "qps", "lat_p50_ms", "lat_p99_ms", "recall10",
        "query_failures", "cache_hit_ratio", "seg_avail_min", "load_s", "failure", "failure_msg", "utc"]
CSV = f"{RDIR}/rows.csv"
if not os.path.exists(CSV):
    open(CSV, "w").write(",".join(COLS) + "\n")

FAILED_N = []   # STOP_ON_FAIL=1: stop the N ladder after the first failing step (2026-10-16)
def emit(row):
    row = {k: row.get(k, "") for k in COLS}
    if row.get("failure"): FAILED_N.append(row.get("N"))
    for k, v in row.items():
        if isinstance(v, float):
            row[k] = round(v, 4)
    with open(CSV, "a") as fh:
        fh.write(",".join(str(v).replace(",", ";") for v in row.values()) + "\n")
    with open(f"{RDIR}/rows.jsonl", "a") as fh:
        fh.write(json.dumps(row) + "\n")
    log("ROW", json.dumps({k: row[k] for k in ("phase", "N", "cap", "cache_pershard", "rss_gb",
        "peak_rss_gb", "seastar_alloc_gb", "cache_extent_gb", "qps", "recall10", "failure")}))

def mrow(m0):
    return {"cache_extent_gb": m0.get("cache_cached_extent_bytes", 0) / G,
            "cache_lru_gb": m0.get("cache_lru_size_bytes", 0) / G,
            "seastar_alloc_gb": m0.get("memory_allocated_memory", 0) / G,
            "seastar_total_gb": m0.get("memory_total_memory", 0) / G,
            "malloc_failed": m0.get("memory_malloc_failed", 0),
            "seg_avail_min": m0.get("seg_avail_min", "")}

def pool_stored(pool):
    r = ceph("df", "detail", "--format", "json", timeout=60)
    try:
        for p in json.loads(r.stdout)["pools"]:
            if p["name"] == pool:
                return p["stats"].get("stored", 0) / G
    except Exception:
        pass
    return ""

# ---- main ---------------------------------------------------------------------------
def main():
    import fcntl
    samp = Sampler(f"{RDIR}/samples.jsonl"); samp.start()
    json.dump({"arm": ARM, "variant": VARIANT, "no_restart": NO_RESTART, "extra_osd": EXTRA_OSD, "cache_fixed": CACHE_FIXED,
               "quantize": QUANT, "cpuset": CPUSET, "client": CLIENT, "idx": IDX,
               "steps": STEPS, "caps": CAPS, "load_cap": LOAD_CAP, "m": M, "nq": NQ,
               "build": BUILD, "vdest": VDEST, "started": now()},
              open(f"{RDIR}/config.json", "w"), indent=1)
    prev_pool = None
    for n in STEPS:
        if FAILED_N and os.environ.get("STOP_ON_FAIL") == "1":
            log(f"STOP_ON_FAIL: N={FAILED_N[0]} failed, not running N>={n}"); break
        base = dict(system="ceph", mode=ARM, N=n, dim=DIM, raw_vec_gb_fp32=n * DIM * 4 / G,
                    raw_vec_gb_repr=n * DIM * (4 if ARM == "fp32" else 1) / G)
        pool = f"mf_{n // 1000}k"
        resume = (RESUME_N == n)
        st = os.statvfs("/home/dev_path")
        if st.f_bavail * st.f_frsize / G < 10:
            emit(base | dict(phase="load", failure="guard_disk", failure_msg="/home/dev_path < 10G free",
                             utc=now())); break
        # ---- load -----------------------------------------------------------------
        if resume:
            # continue a step whose pool is already loaded (after a harness crash)
            prev_pool = pool
            stored = ""
            log(f"RESUME N={n}: pool {pool} already loaded, plan {RESUME_PLAN}")
        else:
            if NO_RESTART and osd_pids() and OSD_ARGS:
                ok, why = True, "no_restart"      # keep our own OSD across N as well
            else:
                # first start always goes through osd_start (pinned, our args); on a
                # fresh cluster this remounts an empty store, which RBM handles fine
                safe_stop(); ok, why = osd_start(LOAD_CAP, CACHE_FIXED or None)
            if not ok:
                emit(base | dict(phase="load", cap=LOAD_CAP, failure=why, utc=now())); break
            if prev_pool:
                ceph("osd", "pool", "rm", prev_pool, prev_pool, "--yes-i-really-really-mean-it")
                log("removed own pool", prev_pool)
            ceph("osd", "pool", "create", pool, "1024", "1024")
            ceph("osd", "pool", "set", pool, "pg_autoscale_mode", "off")
            for _ in range(100):
                s = ceph("pg", "stat").stdout
                if "1024 pgs: 1024 active+clean" in s:
                    break
                time.sleep(3)
            prev_pool = pool
            log(f"load N={n} into {pool}")
            samp.mark(f"load N={n}"); t0 = time.time()
            r = bench("load", pool, n, ["--load-concurrency", "32"], 4 * 3600)
            t1 = time.time()
            L = [l for l in r.stdout.splitlines() if l.startswith("LOAD")]
            lk = kv(L[-1]) if L else {}
            m0 = metrics()
            fail = "" if (lk.get("failures") == "0" and osd_pids()) else "load_failed"
            emit(base | dict(phase="load", cap=LOAD_CAP,
                             cache_pershard=f"{CACHE_FIXED / G:.3f}G" if CACHE_FIXED else "default(2G)",
                             load_s=t1 - t0, pool_stored_gb=pool_stored(pool), failure=fail,
                             failure_msg="" if not fail else (L[-1] if L else r.stderr[-300:]),
                             utc=now()) | mrow(m0) | summ(samp.window(t0, t1)))
            if fail:
                break
            stored = pool_stored(pool)
        # ---- cap sweep --------------------------------------------------------------
        plan = [(c, "default") for c in CAPS] + [(c, "half") for c in CAPS if c in HALF_CAPS]
        if resume and RESUME_PLAN:
            plan = list(RESUME_PLAN)
        if CACHE_FIXED:
            plan = [(c, "fixed") for c in CAPS]
        # User request (2026-10-02): if a cap also fails with the half cache, shrink
        # seastore_cachepin_size_pershard further (1/4, then 1/8 of the per-reactor cap).
        fallback = {"half": "quarter", "quarter": "eighth"} if FALLBACK else {}
        frac = {"half": 2, "quarter": 4, "eighth": 8}
        failed_combo = {}
        i = 0
        while i < len(plan):
            cap, cache = plan[i]; i += 1
            capb = int(cap[:-1]) * G
            if True:
                st = os.statvfs("/home/dev_path")
                if st.f_bavail * st.f_frsize / G < 10:
                    log("disk guard: < 10G free, stopping arm"); osd_stop(); samp.stopped = True; return
                cps = (CACHE_FIXED if cache == "fixed" else
                       None if cache == "default" else capb // 8 // frac[cache])
                tag = dict(cap=cap, cache_pershard="default(2G)" if cps is None else f"{cps / G:.3f}G")
                if NO_RESTART and cap == LOAD_CAP and cps == (CACHE_FIXED or None) and osd_pids():
                    # same cap + cache as the load OSD: measure on the running process.
                    # On RANDOM_BLOCK_SSD a remount after VectorNode writes aborts in
                    # AvlAllocator::_remove_from_tree via RBMCleaner::mark_space_used
                    # (scan_mapped_space) -- a storage bug, not memory (2026-10-02).
                    samp.mark(f"no-restart N={n} cap={cap} cache={cache}")
                    ts = time.time(); ok, why = True, "no_restart"
                else:
                    safe_stop()
                    samp.mark(f"start N={n} cap={cap} cache={cache}")
                    ts = time.time(); ok, why = osd_start(cap, cps)
                if not ok:
                    emit(base | tag | dict(phase="stable", pool_stored_gb=stored, failure=why,
                                           failure_msg=open(f"{EXP}/logs/osd_{ARM}_stdout.log").read()[-400:],
                                           utc=now()) | summ(samp.window(ts, time.time())))
                    if cache in fallback:
                        plan.insert(i, (cap, fallback[cache]))
                    continue
                time.sleep(30); t0 = time.time(); time.sleep(10)
                m0 = metrics()
                emit(base | tag | dict(phase="stable", pool_stored_gb=stored, utc=now())
                     | mrow(m0) | summ(samp.window(t0, time.time())))
                ba0 = bad_alloc_count()
                with open(QUERY_LOCK, "w") as lk_:
                    fcntl.flock(lk_, fcntl.LOCK_EX)
                    samp.mark(f"query N={n} cap={cap} cache={cache}")
                    try:
                        q, err = query(pool, n)
                    finally:
                        fcntl.flock(lk_, fcntl.LOCK_UN)
                m1 = metrics()
                alive = bool(osd_pids())
                if q is None and cache in fallback:
                    plan.insert(i, (cap, fallback[cache]))
                ba = bad_alloc_count() - ba0
                if q is None:
                    kind = ("osd_died" if not alive else
                            "query_hang_bad_alloc" if ("TIMEOUT" in (err or "") and ba > 0) else
                            "query_hang" if "TIMEOUT" in (err or "") else "query_failed")
                    emit(base | tag | dict(phase="query", pool_stored_gb=stored, failure=kind,
                                           failure_msg=f"{err} | new bad_alloc log lines: {ba}",
                                           utc=now()) | mrow(m1) | summ(samp.window(ts, time.time())))
                    continue
                hits = (m1.get("cache_cache_hit", 0) - m0.get("cache_cache_hit", 0))
                acc = (m1.get("cache_cache_access", 0) - m0.get("cache_cache_access", 0))
                fails = int(q.get("failures", "0") or 0)
                fail = ("osd_died" if not alive else
                        "malloc_failed" if m1.get("memory_malloc_failed", 0) > 0 or ba > 0 else
                        "query_failures" if fails else "")
                if fail and cache in fallback:
                    plan.insert(i, (cap, fallback[cache]))
                emit(base | tag | dict(phase="query", pool_stored_gb=stored,
                                       qps=float(q.get("logical_qps", 0)),
                                       lat_p50_ms=float(q.get("logical_latency_us_p50", 0)) / 1000,
                                       lat_p99_ms=float(q.get("logical_latency_us_p99", 0)) / 1000,
                                       recall10=float(q.get("recall_at_top_k", 0)),
                                       query_failures=fails,
                                       cache_hit_ratio=(hits / acc) if acc else "",
                                       failure=fail, utc=now())
                     | mrow(m1) | summ(samp.window(q["_t0"], q["_t1"])))
    osd_stop()
    samp.stopped = True
    log("ARM DONE")

if __name__ == "__main__":
    main()
