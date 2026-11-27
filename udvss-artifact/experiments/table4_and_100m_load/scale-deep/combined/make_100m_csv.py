#!/usr/bin/env python3
"""Merge the 100M (Deep1B) results of Ceph / Milvus / Qdrant / Vespa into one CSV.

Writes  scale_deep_100m_all.csv      (every .row, incl. C-ladder tune rows)
        scale_deep_100m_summary.csv  (main sweep only: mean/min/max over r1..r3)
"""
import csv, glob, os, statistics as st

ROOT = "/home/dev_path/experiments/scale-deep"
OUT = os.path.join(ROOT, "combined")

FQ_KEYS = """mode probe_pg_count subrequests_per_logical_query completed failures first_error
missing_objects duplicate_entries seconds logical_qps logical_latency_us_avg logical_latency_us_p50
logical_latency_us_p95 logical_latency_us_p99 avg_matching_records_per_pg avg_distance_computations_per_pg
avg_total_matching_records_per_query avg_total_distance_computations_per_query recall_at_top_k
rerank_top_n rerank_unmapped avg_returned_results query_batch_size osd_requests osd_query_ops
ops_per_request osd_requests_per_query io_depth avg_queue_wait_us avg_active_us avg_inflight max_inflight""".split()
CEPH_COLS = (["workload", "M", "batch", "conc", "rep", "query_limit", "rounds"] + FQ_KEYS +
             ["pivot_digest", "pivot_build_s", "pivot_reps", "pivot_sse", "pivot_max_cluster",
              "osd_cores", "client_cores", "cpu_window_s", "elapsed_s", "start_utc", "end_utc",
              "git_commit", "status"])

import sys; sys.path.insert(0, "/home/dev_path/corun/scripts")
from run_one import COLS as ENG_COLS  # noqa: E402
# milvus-bench/scripts/run_one.py writes its own layout (one request = nq queries)
MILVUS_COLS = ["dataset", "ef", "nq", "concurrency", "repeat", "queries", "requests", "topk",
               "index_type", "index_M", "index_efConstruction", "metric", "num_shards",
               "replica_number", "completed_vectors", "failures", "first_error",
               "seconds", "qps_vectors", "req_latency_us_avg", "req_latency_us_p50",
               "req_latency_us_p95", "req_latency_us_p99", "per_vector_us_avg",
               "recall_at_10", "recall_scored_queries", "milvus_cpu_cores", "client_cpu_cores",
               "minio_gb", "free_gb", "start_utc", "end_utc", "milvus_version", "status",
               "waves", "min_seconds"]

FIELDS = ["system", "dataset", "N", "knob", "knob_value", "concurrency", "rep", "phase",
          "qps", "recall_at_10", "latency_unit", "lat_avg_ms", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms",
          "queries_completed", "measure_seconds", "candidates_per_query",
          "server_cpu_cores", "client_cpu_cores", "batch_mode", "start_utc", "end_utc", "status",
          "config", "source_file"]

def ms(x):
    try: return round(float(x) / 1000, 3)
    except ValueError: return ""

def read_row(path, cols):
    vals = open(path).read().rstrip("\n").split("\t")
    assert len(vals) == len(cols), (path, len(vals), len(cols))
    return dict(zip(cols, vals))

rows = []
for p in sorted(glob.glob(f"{ROOT}/ceph/results/d100m_pg16384/*.row")):
    r = read_row(p, CEPH_COLS)
    rows.append(dict(
        system="ceph", dataset="deep1b100m", N=100_000_000, knob="M", knob_value=int(r["M"]),
        concurrency=int(r["conc"]), rep=r["rep"], phase="tune" if r["rep"] in ("tune", "rtune") else "sweep",
        qps=float(r["logical_qps"]), recall_at_10=float(r["recall_at_top_k"]),
        latency_unit=f"batch of {r['batch']} queries",
        lat_avg_ms=ms(r["logical_latency_us_avg"]), lat_p50_ms=ms(r["logical_latency_us_p50"]),
        lat_p95_ms=ms(r["logical_latency_us_p95"]), lat_p99_ms=ms(r["logical_latency_us_p99"]),
        queries_completed=int(r["completed"]), measure_seconds=float(r["seconds"]),
        candidates_per_query=r["avg_total_matching_records_per_query"],
        server_cpu_cores=r["osd_cores"], client_cpu_cores=r["client_cores"],
        batch_mode=f"native_batch_{r['batch']}", start_utc=r["start_utc"], end_utc=r["end_utc"],
        status=r["status"], config="1 OSD (cpuset 0-7), PG16384, balanced pivot",
        source_file=os.path.relpath(p, ROOT)))

for p in sorted(glob.glob(f"{ROOT}/engines/results/milvus/deep1b100m/*.row")):
    r = read_row(p, MILVUS_COLS)
    rows.append(dict(
        system="milvus", dataset=r["dataset"], N=100_000_000, knob="ef", knob_value=int(r["ef"]),
        concurrency=int(r["concurrency"]), rep=r["repeat"],
        phase="tune" if r["repeat"] in ("tune", "rtune") else "sweep",
        qps=float(r["qps_vectors"]), recall_at_10=float(r["recall_at_10"]),
        latency_unit=f"batch of {r['nq']} queries",
        lat_avg_ms=ms(r["req_latency_us_avg"]), lat_p50_ms=ms(r["req_latency_us_p50"]),
        lat_p95_ms=ms(r["req_latency_us_p95"]), lat_p99_ms=ms(r["req_latency_us_p99"]),
        queries_completed=int(r["completed_vectors"]), measure_seconds=float(r["seconds"]),
        candidates_per_query="", server_cpu_cores=r["milvus_cpu_cores"],
        client_cpu_cores=r["client_cpu_cores"], batch_mode=f"native_batch_{r['nq']}",
        start_utc=r["start_utc"], end_utc=r["end_utc"], status=r["status"],
        config=f"milvus {r['milvus_version']} HNSW M{r['index_M']} efc{r['index_efConstruction']}, cpuset 0-7",
        source_file=os.path.relpath(p, ROOT)))

for eng in ("qdrant", "vespa"):
    for p in sorted(glob.glob(f"{ROOT}/engines/results/{eng}/deep1b100m/*.row")):
        r = read_row(p, ENG_COLS)
        unit = int(r["unit_vectors"])
        rows.append(dict(
            system=eng, dataset=r["dataset"], N=100_000_000, knob="ef", knob_value=int(r["ef"]),
            concurrency=int(r["concurrency"]), rep=r["repeat"],
            phase="tune" if r["repeat"] in ("tune", "rtune") else "sweep",
            qps=float(r["qps_vectors"]), recall_at_10=float(r["recall_at_10"]),
            latency_unit="1 query" if unit == 1 else f"batch of {unit} queries",
            lat_avg_ms=ms(r["unit_latency_us_avg"]), lat_p50_ms=ms(r["unit_latency_us_p50"]),
            lat_p95_ms=ms(r["unit_latency_us_p95"]), lat_p99_ms=ms(r["unit_latency_us_p99"]),
            queries_completed=int(r["completed_vectors"]), measure_seconds=float(r["seconds"]),
            candidates_per_query="", server_cpu_cores=r["engine_cpu_cores"],
            client_cpu_cores=r["client_cpu_cores"], batch_mode=r["batch_mode"],
            start_utc=r["start_utc"], end_utc=r["end_utc"], status=r["status"],
            config=f"{eng} {r['engine_version']} HNSW M{r['index_M']} efc{r['index_efConstruction']}, "
                   f"cpuset {'12-19' if eng == 'vespa' else '0-7'}",
            source_file=os.path.relpath(p, ROOT)))

order = {"ceph": 0, "milvus": 1, "qdrant": 2, "vespa": 3}
rows.sort(key=lambda r: (order[r["system"]], r["phase"] != "sweep", r["knob_value"],
                         r["concurrency"], r["rep"]))
with open(f"{OUT}/scale_deep_100m_all.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, FIELDS); w.writeheader(); w.writerows(rows)

# summary: main sweep (r1..r3) per system x knob
groups = {}
for r in rows:
    if r["phase"] == "sweep" and r["status"] == "OK":
        groups.setdefault((r["system"], r["knob"], r["knob_value"], r["concurrency"]), []).append(r)
SF = ["system", "knob", "knob_value", "concurrency", "reps", "qps_mean", "qps_min", "qps_max",
      "recall_at_10_mean", "lat_p50_ms_mean", "lat_p99_ms_mean", "latency_unit",
      "candidates_per_query_mean", "server_cpu_cores_mean"]
def mean(xs):
    xs = [float(x) for x in xs if x not in ("", "NA")]
    return round(st.mean(xs), 4) if xs else ""
with open(f"{OUT}/scale_deep_100m_summary.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, SF); w.writeheader()
    for k, g in groups.items():
        q = [x["qps"] for x in g]
        w.writerow(dict(system=k[0], knob=k[1], knob_value=k[2], concurrency=k[3], reps=len(g),
                        qps_mean=round(st.mean(q), 1), qps_min=round(min(q), 1), qps_max=round(max(q), 1),
                        recall_at_10_mean=mean(x["recall_at_10"] for x in g),
                        lat_p50_ms_mean=mean(x["lat_p50_ms"] for x in g),
                        lat_p99_ms_mean=mean(x["lat_p99_ms"] for x in g),
                        latency_unit=g[0]["latency_unit"],
                        candidates_per_query_mean=mean(x["candidates_per_query"] for x in g),
                        server_cpu_cores_mean=mean(x["server_cpu_cores"] for x in g)))
print(len(rows), "rows;", len(groups), "summary rows")
