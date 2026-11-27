#!/usr/bin/env python3
"""One case of the Milvus sweep: (dataset, ef, C) -> one .row file.

Mirrors experiments/workload-bench/scripts/run_one.sh.  Fixed: nq=1024 (Ceph's
B=1024), topK=10, L2, HNSW M=30/efConstruction=360, shard 1, replica 1.  The
query count follows the Ceph rule queries = C * nq * 4, so a case at C=80 issues
eight times the queries of one at C=10, exactly as on the Ceph side.

QPS is query VECTORS per second on both sides.  Latency percentiles are
per REQUEST (each request carries nq vectors), which is what the Ceph rows also
report at B=1024 -- their p50 at M=16/C=10 is 5.6 s for the same reason.

recall is deterministic for a fixed (dataset, ef), so it is scored over the
first pass through the query set only and the rest of the run is pure
throughput; recall_scored_queries records how many were actually checked.
"""
import argparse, json, multiprocessing as mp, os, sys, threading, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/home/dev_path/milvus-motivation/scripts")
from lib.dataset import open_dataset, read_fvecs, read_ivecs
import bench_env as E
from pymilvus import MilvusClient

COLS = ["dataset", "ef", "nq", "concurrency", "repeat", "queries", "requests", "topk",
        "index_type", "index_M", "index_efConstruction", "metric", "num_shards",
        "replica_number", "completed_vectors", "failures", "first_error",
        "seconds", "qps_vectors", "req_latency_us_avg", "req_latency_us_p50",
        "req_latency_us_p95", "req_latency_us_p99", "per_vector_us_avg",
        "recall_at_10", "recall_scored_queries", "milvus_cpu_cores", "client_cpu_cores",
        "minio_gb", "free_gb", "start_utc", "end_utc", "milvus_version", "status",
        # appended after "status" so the 34-column rows written before waves
        # existed still parse; readers treat a missing value as waves=4.
        "waves",
        # appended 2026-10-10 (scale-deep, measure >= 2 min). 0 / missing =
        # the old count-bound run; > 0 = every worker keeps issuing requests until
        # this many seconds have passed, then finishes the request it is on.
        "min_seconds"]

# --- client workers ---------------------------------------------------------
#
# Separate PROCESSES, not threads. One Python process is GIL-bound at ~1.1 cores,
# and this harness reaches that at the fast end of the curve: measured on the
# HNSW sweep, nytimes ef=10 ran at client 1.02 cores with the ENGINE at only
# 7.30/8, and deep1m ef=10 at client 0.92 with the engine at 6.94/8 -- the engine
# had headroom the client could not feed. IVF_FLAT at low nprobe is faster still,
# so most of the nprobe curve would have been measuring this client.
#
# Layout is P processes x T threads with P*T = concurrency, so in-flight requests
# and the request-to-worker assignment are exactly what the threaded version had.
# client_cpu_seconds() already counts children, so the CPU column stays correct.
_W = {}

def _init_worker(dataset, ef, score_until):
    global _W
    ds = open_dataset(dataset, E.DATASET_DIR)
    q = read_fvecs(ds.query_path)
    _W = dict(c=MilvusClient(uri=E.URI), name=E.collection_name(dataset),
              sp={"params": {E.KNOB_KEY: ef}}, q=q, gt=read_ivecs(ds.gt_path),
              nq_total=len(q), score_until=score_until)

def _ready(_):
    return len(_W)

def _slice_for(r):
    lo = (r * E.NQ) % _W["nq_total"]
    idx = np.arange(lo, lo + E.NQ) % _W["nq_total"]
    return idx, _W["q"][idx]

def _worker_requests(spec):
    """Request numbers for one worker: a fixed list, or (time-bound) w, w+C, w+2C, ...
    until the shared deadline passes."""
    if isinstance(spec, list):
        yield from spec
        return
    w, conc, deadline = spec
    r = w
    while time.time() < deadline:
        yield r
        r += conc

def _run_chunks(chunks):
    """One client process: its share of the C workers, as threads inside it."""
    c, name, sp = _W["c"], _W["name"], _W["sp"]
    gt, score_until = _W["gt"], _W["score_until"]
    lat, agg, lock = [], {"hits": 0, "scored": 0, "failures": 0, "first_error": ""}, threading.Lock()

    def work(rs):
        local = []
        for r in _worker_requests(rs):
            idx, v = _slice_for(r)
            t0 = time.perf_counter()
            try:
                res = c.search(name, data=v.tolist(), limit=E.TOPK,
                               search_params=sp, anns_field="vector")
            except Exception as exc:
                with lock:
                    agg["failures"] += 1
                    if not agg["first_error"]:
                        # newlines too: a multi-line gRPC error would otherwise
                        # split the row across several lines of the TSV.
                        agg["first_error"] = " ".join(str(exc)[:200].split())
                continue
            local.append((time.perf_counter() - t0) * 1e6)
            if r < score_until:
                h = 0
                for j, one in enumerate(res):
                    h += len({m["id"] for m in one} & set(gt[idx[j]][:E.TOPK].tolist()))
                with lock:
                    agg["hits"] += h
                    agg["scored"] += len(res)
        with lock:
            lat.extend(local)

    ts = [threading.Thread(target=work, args=(rs,)) for rs in chunks]
    for t in ts: t.start()
    for t in ts: t.join()
    return lat, agg

def client_cpu_seconds():
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--ef", type=int, required=True)
    ap.add_argument("--concurrency", type=int, required=True)
    ap.add_argument("--repeat", default="1")
    ap.add_argument("--queries", type=int, default=None)
    ap.add_argument("--waves", type=int, default=4,
                    help="requests per worker; queries = C*nq*waves.  4 is what the "
                         "Ceph rule ceil(C*batch*4/query_limit) works out to, and is "
                         "kept for the finals; the C search and the ef curve use 2.")
    ap.add_argument("--client-procs", type=int, default=0,
                    help="client PROCESSES (0 = auto: min(C, $MVB_CLIENT_PROCS or 8))")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--min-seconds", type=float, default=0,
                    help="time-bound run: every worker issues requests until this many "
                         "seconds have passed (0 = count-bound, queries = C*nq*waves)")
    a = ap.parse_args()

    ds = open_dataset(a.dataset, E.DATASET_DIR)
    name = E.collection_name(a.dataset)
    out_dir = a.out_dir or f"{E.RESULTS}/{a.dataset}"
    os.makedirs(out_dir, exist_ok=True)
    wtag = "" if a.waves == 4 else f"_w{a.waves}"
    if a.min_seconds > 0:
        wtag = f"_t{int(a.min_seconds)}"
    row_path = f"{out_dir}/ef{a.ef}_nq{E.NQ}_c{a.concurrency}{wtag}_r{a.repeat}.row"

    queries = a.queries or a.concurrency * E.NQ * a.waves
    n_req = max(1, queries // E.NQ)
    q = read_fvecs(ds.query_path)
    gt = read_ivecs(ds.gt_path)
    nq_total = len(q)

    c = MilvusClient(uri=E.URI)
    sp = {"params": {E.KNOB_KEY: a.ef}}   # "ef" for HNSW, "nprobe" for IVF_FLAT

    # one request's worth of query rows, cycling the query set
    def slice_for(r):
        lo = (r * E.NQ) % nq_total
        idx = np.arange(lo, lo + E.NQ) % nq_total
        return idx, q[idx]

    # warm-up: one request per worker, discarded
    for r in range(a.concurrency):
        _, v = slice_for(r)
        c.search(name, data=v.tolist(), limit=E.TOPK, search_params=sp, anns_field="vector")

    score_until = (nq_total + E.NQ - 1) // E.NQ     # first pass over the query set

    chunks = [list(range(w, n_req, a.concurrency)) for w in range(a.concurrency)]
    nproc = a.client_procs or min(a.concurrency, int(os.environ.get("MVB_CLIENT_PROCS", 8)))
    nproc = max(1, min(nproc, a.concurrency))
    per_proc = [g for g in ([chunks[k::nproc] for k in range(nproc)]) if g]

    pool = mp.get_context("fork").Pool(len(per_proc), initializer=_init_worker,
                                       initargs=(a.dataset, a.ef, score_until))
    pool.map(_ready, range(len(per_proc)))   # fork + connect BEFORE the clock starts

    if a.min_seconds > 0:
        # one (worker, C, deadline) spec per worker; the deadline is set just before
        # the clock starts, so every worker shares it.
        deadline = time.time() + 2 + a.min_seconds
        specs = [(w, a.concurrency, deadline) for w in range(a.concurrency)]
        per_proc = [g for g in ([specs[k::nproc] for k in range(nproc)]) if g]
        time.sleep(max(0.0, deadline - a.min_seconds - time.time()))
    cpu0, mv0 = client_cpu_seconds(), E.milvus_cpu_usec()
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0 = time.perf_counter()
    parts = pool.map(_run_chunks, per_proc)
    wall = time.perf_counter() - t0
    pool.close(); pool.join()                # reap so os.times() counts their CPU

    lat, hits, scored, failures, first_error = [], 0, 0, 0, ""
    for pl, pa in parts:
        lat.extend(pl); hits += pa["hits"]; scored += pa["scored"]
        failures += pa["failures"]
        if not first_error:
            first_error = pa["first_error"]
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    mv1, cpu1 = E.milvus_cpu_usec(), client_cpu_seconds()

    done_req = len(lat)
    if a.min_seconds > 0:
        queries, n_req = done_req * E.NQ, done_req
    done_vec = done_req * E.NQ
    p = lambda k: float(np.percentile(lat, k)) if lat else float("nan")
    row = {
        "dataset": a.dataset, "ef": a.ef, "nq": E.NQ, "concurrency": a.concurrency,
        "repeat": a.repeat, "queries": queries, "requests": n_req, "topk": E.TOPK,
        "index_type": E.INDEX_TYPE,
        # IVF_FLAT has ONE build parameter, so nlist goes in index_M and
        # index_efConstruction stays empty; index_type says which to read. The
        # `ef` column likewise carries nprobe on an IVF row.
        "index_M": E.INDEX_PARAMS.get("M", E.INDEX_PARAMS.get("nlist", "")),
        "index_efConstruction": E.INDEX_PARAMS.get("efConstruction", ""), "metric": E.METRIC,
        "num_shards": E.NUM_SHARDS, "replica_number": E.REPLICA_NUMBER,
        "completed_vectors": done_vec, "failures": failures, "first_error": first_error,
        "seconds": round(wall, 3), "qps_vectors": round(done_vec / wall, 3),
        "req_latency_us_avg": round(float(np.mean(lat)), 1) if lat else float("nan"),
        "req_latency_us_p50": round(p(50), 1), "req_latency_us_p95": round(p(95), 1),
        "req_latency_us_p99": round(p(99), 1),
        "per_vector_us_avg": round(float(np.mean(lat)) / E.NQ, 2) if lat else float("nan"),
        "recall_at_10": round(hits / (scored * E.TOPK), 6) if scored else float("nan"),
        "recall_scored_queries": scored,
        "milvus_cpu_cores": round((mv1 - mv0) / 1e6 / wall, 3) if (mv0 and mv1) else "NA",
        "client_cpu_cores": round((cpu1 - cpu0) / wall, 3),
        "minio_gb": round(E.host_volume_gb("minio"), 3), "free_gb": round(E.host_df_gb(), 2),
        "start_utc": start, "end_utc": end, "milvus_version": c.get_server_version(),
        "status": "OK" if failures == 0 else "FAILED", "waves": a.waves,
        "min_seconds": a.min_seconds,
    }
    with open(row_path, "w") as fh:
        fh.write("\t".join(str(row[k]) for k in COLS) + "\n")
    print(f"{a.dataset} ef={a.ef} C={a.concurrency} rep={a.repeat} -> {row['status']} "
          f"qps={row['qps_vectors']:.1f} recall={row['recall_at_10']} "
          f"p50={row['req_latency_us_p50']/1000:.1f}ms milvus_cpu={row['milvus_cpu_cores']} "
          f"client_cpu={row['client_cpu_cores']} failures={failures} {wall:.1f}s")
    print(f"  row={row_path}")
    return 0 if failures == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
