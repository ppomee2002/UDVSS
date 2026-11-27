#!/usr/bin/env python3
"""Steps G/H/I. The measurement harness.

One invocation = one row of Table 2 (and one row of Table 3, and rows of Table 4).
NQ is 1 and is not configurable -- the whole study is defined at NQ=1.

It records, for a single fixed condition:
  * end-to-end latency per query, client side, in microseconds -> raw CSV
  * mean / p50 / p95 / p99 / stddev
  * S, N, M measured from counter deltas (never from parameters)
  * per-hop RX/TX bytes per query from each pod's own netns, idle-baseline subtracted
  * the Milvus-internal latency breakdown from histogram deltas
  * optionally recall@k against the shipped ground truth

    08_bench.py --collection mm_deep1m_s4_c8 --label "S4_N1_M8" \
                --shards 4 --querynode-pods 1 --insert-chunks 8 \
                --queries 3000 --warmup 500
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from pymilvus import MilvusClient, connections, utility

from lib.common import RunConfig, list_pods, net_group, pods_by_component, write_csv, write_json, append_csv, summarize
from lib.dataset import open_dataset, read_fvecs, read_ivecs, recall_at_k
from lib import milvus_metrics as mm
from lib import milvus_control as mc
from lib import podnet

RUNS_CSV_FIELDS = [
    # --- condition (identical across every row of a table; recorded on every row) ---
    "label", "started_at", "dataset", "vectors", "dim", "metric", "index_type",
    "index_params", "search_params", "topk", "nq", "output_fields", "concurrency", "warmup", "queries",
    "collection", "milvus_version", "chart_version", "physical_nodes",
    # --- the three knobs, as SET ---
    "param_S_num_shards", "param_N_querynode_pods", "param_M_insert_chunks",
    "param_replica_number",
    # --- the three knobs, as MEASURED (primary = the supervisor's definitions:
    #     N is per shard, M is per QueryNode per shard) ---
    "actual_shards_touched", "actual_N_per_shard_avg", "actual_M_per_worker_per_shard",
    # --- whole-query aggregates: reported, but NOT to be called N or M ---
    "actual_N_unique_total", "actual_M_total_searched",
    "actual_worker_rpcs_per_query",
    "actual_sealed_segments_per_query", "actual_growing_segments_per_query",
    # --- per-pod detail and control-plane cross-check ---
    "actual_M_per_worker_by_pod", "actual_shards_served_by_pod",
    "actual_shards_served_min", "actual_shards_served_max", "querynode_pods_hit",
    "cp_N_per_shard_min", "cp_N_per_shard_avg", "cp_N_per_shard_max", "cp_shard_nodes",
    "cp_segments_per_node_min", "cp_segments_per_node_avg", "cp_segments_per_node_max",
    # --- latency (client side, authoritative) ---
    "mean_us", "p50_us", "p95_us", "p99_us", "stddev_us", "min_us", "max_us", "qps",
    # --- network per query ---
    "rx_kb_per_query_proxy", "tx_kb_per_query_proxy",
    "rx_kb_per_query_streaming", "tx_kb_per_query_streaming",
    "rx_kb_per_query_query", "tx_kb_per_query_query",
    "rx_kb_per_query_total", "tx_kb_per_query_total",
    # Per-netns eth0 (pod veth) and lo, grouped by netns. 'snqn' is the loopback
    # arm's shared StreamingNode+QueryNode pod. None of these is physical NIC
    # traffic; see lib/podnet.py. lo is bytes that crossed loopback (lo tx).
    "eth0_rx_kb_per_query_snqn", "eth0_tx_kb_per_query_snqn",
    "lo_kb_per_query_proxy", "lo_kb_per_query_streaming", "lo_kb_per_query_query",
    "lo_kb_per_query_snqn", "lo_kb_per_query_total",
    "sum_pod_eth0_rxtx_kb_per_query", "net_netns_groups",
    "app_client_to_proxy_bytes_per_query", "app_proxy_to_client_bytes_per_query",
    "app_search_req_bytes_at_delegator_per_query",
    "net_baseline_source", "net_underflow_pods", "net_baseline_dominated_pods",
    # --- latency breakdown (ms; *_coarse columns flag integer-ms metrics) ---
    "proxy_e2e_ms", "proxy_wait_shards_ms", "proxy_reduce_ms", "proxy_decode_ms",
    "deleg_wait_tsafe_ms", "deleg_reduce_shards_ms",
    "worker_rpc_ms", "worker_queue_ms", "worker_seg_search_ms", "worker_segcore_ms",
    "worker_reduce_segs_ms", "coarse_metrics_note",
    # --- quality / bookkeeping ---
    "recall_at_k", "errors", "wall_seconds", "raw_csv", "run_json", "notes",
]


def load_idle_rate(path: str | None) -> tuple[dict | None, str]:
    cand = path or os.path.join(os.environ.get("MM_RESULTS", "."), "baseline", "latest", "idle_net_rate.json")
    if os.path.exists(cand):
        with open(cand) as fh:
            d = json.load(fh)
        return d.get("rate_bytes_per_s"), cand
    return None, "NONE (run 04_baseline_idle.sh; per-hop bytes are raw totals)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default=os.environ.get("MM_URI"))
    ap.add_argument("--collection", required=True)
    ap.add_argument("--label", required=True, help="row label, e.g. S4_N2_M8")
    ap.add_argument("--dataset", default=os.environ.get("MM_DATASET", "deep1m"))
    ap.add_argument("--queries", type=int, default=int(os.environ.get("MM_QUERIES", 3000)))
    ap.add_argument("--warmup", type=int, default=int(os.environ.get("MM_WARMUP", 500)))
    ap.add_argument("--topk", type=int, default=int(os.environ.get("MM_TOPK", 10)))
    ap.add_argument("--output-fields", default=os.environ.get("MM_OUTPUT_FIELDS", ""),
                    help="comma-separated fields to materialise in the result, e.g. 'vector'. "
                         "Empty (default) = ids+distances only, so the segment never touches the "
                         "raw vector payload. Setting it to the anns field forces the post-ANN "
                         "raw-vector read, which is how Motivation 2 separates index read from "
                         "vector read. Changes the wire payload, so compare traffic too.")
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("MM_CONCURRENCY", 1)))
    ap.add_argument("--search-params", default=os.environ.get("MM_SEARCH_PARAMS", '{"ef":64}'))
    ap.add_argument("--index", default=os.environ.get("MM_INDEX", "HNSW"))
    ap.add_argument("--index-params", default=os.environ.get("MM_INDEX_PARAMS", '{"M":16,"efConstruction":200}'))
    ap.add_argument("--metric", default=os.environ.get("MM_METRIC", "L2"))
    # the knobs as set, recorded so "set" and "measured" sit side by side
    ap.add_argument("--shards", type=int, required=True)
    ap.add_argument("--querynode-pods", type=int, required=True)
    ap.add_argument("--insert-chunks", type=int, required=True)
    ap.add_argument("--replica-number", type=int, default=1)
    ap.add_argument("--recall", action="store_true", help="score recall@topk against the shipped GT")
    ap.add_argument("--idle-rate", default=None)
    ap.add_argument("--runs-csv", default=None)
    ap.add_argument("--notes", default="")
    a = ap.parse_args()

    if not a.uri:
        print("set --uri or MM_URI", file=sys.stderr)
        return 2
    NQ = 1  # fixed by the study design

    results_root = os.environ.get("MM_RESULTS", ".")
    runs_csv = a.runs_csv or os.path.join(results_root, "runs.csv")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw_csv = os.path.join(results_root, "raw", f"{a.label}-{stamp}.csv")
    run_json = os.path.join(results_root, "runs", f"{a.label}-{stamp}.json")

    ds = open_dataset(a.dataset)
    need = a.warmup + a.queries
    avail = int(ds.meta.get("query_count") or 0)
    qv = read_fvecs(ds.query_path, 0, min(need, avail) if avail else need)
    if len(qv) < need:
        print(f"[note] only {len(qv)} query vectors available for {need} probes; cycling them")
    gt = read_ivecs(ds.gt_path) if a.recall else None
    sp = json.loads(a.search_params)
    out_fields = [f for f in a.output_fields.split(",") if f.strip()] or None

    client = MilvusClient(uri=a.uri)
    connections.connect(alias="mm", uri=a.uri)
    pods = list_pods()
    bycomp = pods_by_component(pods)
    node_of = {p.name: p.node for p in pods}
    comp_of = {p.name: p.component for p in pods}
    group_of = net_group(pods)       # netns -> proxy/streamingnode/querynode/snqn/...

    print(f"collection={a.collection} label={a.label}")
    print(f"pods: " + ", ".join(f"{c}x{len(v)}" for c, v in sorted(bycomp.items())))
    print(f"NQ={NQ} topk={a.topk} output_fields={out_fields} conc={a.concurrency} warmup={a.warmup} queries={a.queries}")

    def probe(i: int):
        v = qv[i % len(qv)]
        return client.search(a.collection, data=[v.tolist()], limit=a.topk,
                             search_params=sp, anns_field="vector",
                             output_fields=out_fields)

    # ---------------------------------------------------------------- warmup
    print("warmup ...")
    for i in range(a.warmup):
        probe(i)

    idle_rate, idle_src = load_idle_rate(a.idle_rate)

    # ---------------------------------------------------------------- measure
    print("measuring ...")
    lat_us: List[float] = []
    ret_ids: List[List[int]] = []
    q_index: List[int] = []
    errors = 0
    lock = threading.Lock()

    net_before = podnet.NetSnapshot.take(pods)
    met_before = mm.Snapshot.take(pods)
    t_start = time.perf_counter()

    def one(i: int):
        nonlocal errors
        idx = a.warmup + i
        t0 = time.perf_counter_ns()
        try:
            res = probe(idx)
        except Exception as exc:
            with lock:
                errors += 1
            if errors < 5:
                print(f"  [err] query {i}: {exc}")
            return
        t1 = time.perf_counter_ns()
        ids = [int(h["id"]) for h in res[0]] if res and res[0] else []
        with lock:
            lat_us.append((t1 - t0) / 1000.0)
            ret_ids.append(ids)
            q_index.append(idx % len(qv))

    if a.concurrency == 1:
        for i in range(a.queries):
            one(i)
    else:
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            list(ex.map(one, range(a.queries)))

    wall = time.perf_counter() - t_start
    met_after = mm.Snapshot.take(pods)
    net_after = podnet.NetSnapshot.take(pods)

    n_ok = len(lat_us)
    if n_ok == 0:
        print("no successful queries", file=sys.stderr)
        return 1

    # ---------------------------------------------------------------- derive
    stats = summarize(lat_us)
    snm = mm.measure_snm(met_before, met_after, n_ok)
    # Control-plane declaration, taken after the run so it describes the state that
    # actually served the queries. It is an UPPER BOUND on participation.
    cp_shards = mc.shard_topology(a.collection, client=client)
    cp_segs = mc.segment_placement(a.collection)
    bd = mm.measure_breakdown(met_before, met_after)
    appb = mm.measure_app_bytes(met_before, met_after)

    win = net_after.t - net_before.t
    ndelta = podnet.delta(net_before, net_after)
    npq = podnet.per_query(ndelta, idle_rate, n_ok, win)
    npq_lo = podnet.per_query(podnet.delta(net_before, net_after, "lo"), idle_rate, n_ok, win, iface="lo")
    per_comp: Dict[str, Dict[str, float]] = {}
    lo_comp: Dict[str, float] = {}
    underflow = []
    baseline_bad = []
    for pod, v in npq.items():
        c = group_of.get(pod, "?")
        d = per_comp.setdefault(c, {"rx": 0.0, "tx": 0.0})
        d["rx"] += v["rx_bytes_per_query"]
        d["tx"] += v["tx_bytes_per_query"]
        if v["rx_underflow"] or v["tx_underflow"]:
            underflow.append(pod)
        # Only meaningful on the query path. mixcoord/datanode carry no per-query
        # traffic, so their measured bytes are background by definition and the flag
        # would fire on every run.
        if c in ("proxy", "streamingnode", "querynode", "snqn") and (
                v.get("rx_baseline_dominates") or v.get("tx_baseline_dominates")):
            baseline_bad.append(pod)
    for pod, v in npq_lo.items():
        # lo tx == lo rx for the same packets; tx alone is the bytes that crossed.
        c = group_of.get(pod, "?")
        lo_comp[c] = lo_comp.get(c, 0.0) + v["tx_bytes_per_query"]
        if v["tx_underflow"]:
            underflow.append(f"{pod}:lo")

    recall = float("nan")
    if a.recall and gt is not None:
        pairs = [(q_index[i], ret_ids[i]) for i in range(n_ok) if ret_ids[i]]
        if pairs:
            R = np.array([p[1][:a.topk] + [-1] * (a.topk - len(p[1])) for p in pairs])
            G = np.array([gt[p[0]][:a.topk] for p in pairs])
            recall = recall_at_k(R, G, a.topk)

    coarse = [k[:-7] for k in bd if k.endswith("_coarse")]

    row = {
        "label": a.label,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dataset": ds.name, "vectors": ds.count, "dim": ds.dim, "metric": a.metric,
        "index_type": a.index, "index_params": a.index_params, "search_params": a.search_params,
        "topk": a.topk, "nq": NQ, "output_fields": a.output_fields,
        "concurrency": a.concurrency,
        "warmup": a.warmup, "queries": n_ok,
        "collection": a.collection,
        "milvus_version": os.environ.get("MM_IMAGE_TAG", "v2.6.23"),
        "chart_version": os.environ.get("MM_CHART_VERSION", "5.0.28"),
        "physical_nodes": 3,
        "param_S_num_shards": a.shards,
        "param_N_querynode_pods": a.querynode_pods,
        "param_M_insert_chunks": a.insert_chunks,
        "param_replica_number": a.replica_number,
        **snm.as_row(),
        "mean_us": round(stats["mean_us"], 2),
        "p50_us": round(stats["p50_us"], 2),
        "p95_us": round(stats["p95_us"], 2),
        "p99_us": round(stats["p99_us"], 2),
        "stddev_us": round(stats["stddev_us"], 2),
        "min_us": round(stats["min_us"], 2),
        "max_us": round(stats["max_us"], 2),
        "qps": round(n_ok / wall, 2),
        "rx_kb_per_query_proxy": round(per_comp.get("proxy", {}).get("rx", 0) / 1024, 4),
        "tx_kb_per_query_proxy": round(per_comp.get("proxy", {}).get("tx", 0) / 1024, 4),
        "rx_kb_per_query_streaming": round(per_comp.get("streamingnode", {}).get("rx", 0) / 1024, 4),
        "tx_kb_per_query_streaming": round(per_comp.get("streamingnode", {}).get("tx", 0) / 1024, 4),
        "rx_kb_per_query_query": round(per_comp.get("querynode", {}).get("rx", 0) / 1024, 4),
        "tx_kb_per_query_query": round(per_comp.get("querynode", {}).get("tx", 0) / 1024, 4),
        "rx_kb_per_query_total": round(sum(d["rx"] for d in per_comp.values()) / 1024, 4),
        "tx_kb_per_query_total": round(sum(d["tx"] for d in per_comp.values()) / 1024, 4),
        "eth0_rx_kb_per_query_snqn": round(per_comp.get("snqn", {}).get("rx", 0) / 1024, 4),
        "eth0_tx_kb_per_query_snqn": round(per_comp.get("snqn", {}).get("tx", 0) / 1024, 4),
        "lo_kb_per_query_proxy": round(lo_comp.get("proxy", 0) / 1024, 4),
        "lo_kb_per_query_streaming": round(lo_comp.get("streamingnode", 0) / 1024, 4),
        "lo_kb_per_query_query": round(lo_comp.get("querynode", 0) / 1024, 4),
        "lo_kb_per_query_snqn": round(lo_comp.get("snqn", 0) / 1024, 4),
        "lo_kb_per_query_total": round(sum(lo_comp.values()) / 1024, 4),
        # every measured pod's eth0 rx+tx added up. A hop between two measured pods
        # is in here twice, and same-host hops are in here at all -- this is NOT the
        # bytes on any physical wire.
        "sum_pod_eth0_rxtx_kb_per_query": round(sum(d["rx"] + d["tx"] for d in per_comp.values()) / 1024, 4),
        "net_netns_groups": ";".join(f"{g}@{k}" for k, g in sorted(group_of.items())),
        "app_client_to_proxy_bytes_per_query": round(appb["app_client_to_proxy_bytes"] / n_ok, 2),
        "app_proxy_to_client_bytes_per_query": round(appb["app_proxy_to_client_bytes"] / n_ok, 2),
        "app_search_req_bytes_at_delegator_per_query": round(appb["app_search_req_bytes_at_delegator"] / n_ok, 2),
        "cp_N_per_shard_min": cp_shards.get("cp_N_per_shard_min", ""),
        "cp_N_per_shard_avg": cp_shards.get("cp_N_per_shard_avg", ""),
        "cp_N_per_shard_max": cp_shards.get("cp_N_per_shard_max", ""),
        "cp_shard_nodes": cp_shards.get("cp_shard_nodes", ""),
        "cp_segments_per_node_min": cp_segs.get("cp_segments_per_node_min", ""),
        "cp_segments_per_node_avg": cp_segs.get("cp_segments_per_node_avg", ""),
        "cp_segments_per_node_max": cp_segs.get("cp_segments_per_node_max", ""),
        "net_baseline_source": idle_src,
        "net_underflow_pods": ";".join(sorted(underflow)),
        "net_baseline_dominated_pods": ";".join(sorted(baseline_bad)),
        "coarse_metrics_note": "integer-ms (+-1ms): " + ",".join(sorted(coarse)),
        "recall_at_k": round(recall, 4) if recall == recall else "",
        "errors": errors,
        "wall_seconds": round(wall, 3),
        "raw_csv": raw_csv,
        "run_json": run_json,
        "notes": a.notes,
    }
    for k in ("proxy_e2e", "proxy_wait_shards", "proxy_reduce", "proxy_decode",
              "deleg_wait_tsafe", "deleg_reduce_shards", "worker_rpc", "worker_queue",
              "worker_seg_search", "worker_segcore", "worker_reduce_segs"):
        v = bd.get(f"{k}_ms")
        row[f"{k}_ms"] = round(v, 4) if v is not None else ""

    # ---------------------------------------------------------------- persist
    write_csv(raw_csv, [
        {"i": i, "query_row": q_index[i], "latency_us": round(lat_us[i], 3),
         "returned_ids": " ".join(map(str, ret_ids[i]))}
        for i in range(n_ok)
    ])
    write_json(run_json, {
        "row": row,
        "snm_detail": {
            "S_actual": snm.S_actual,
            "N_per_shard_avg": snm.N_per_shard_avg,
            "M_per_worker_per_shard": snm.M_per_worker_per_shard,
            "N_unique_total": snm.N_unique_total,
            "M_total_searched": snm.M_total_searched,
            "shard_reduces_by_pod": snm.shard_reduces_by_pod,
            "worker_rpcs_by_pod": snm.worker_rpcs_by_pod,
            "sealed_by_pod": snm.sealed_segment_searches_by_pod,
            "growing_by_pod": snm.growing_segment_searches_by_pod,
            "M_per_worker_per_shard_by_pod": snm.M_per_worker_per_shard_by_pod,
            "shards_served_by_pod": snm.shards_served_by_pod,
        },
        "control_plane": {"shards": cp_shards, "segments": cp_segs},
        "breakdown_raw": bd,
        "net_per_pod": npq,
        "net_lo_per_pod": npq_lo,
        "net_window_seconds": net_after.t - net_before.t,
        "pods": {p.name: {"component": p.component, "node": p.node, "ip": p.ip} for p in pods},
    })
    append_csv(runs_csv, row, RUNS_CSV_FIELDS)

    # ---------------------------------------------------------------- report
    print()
    print(f"  latency us   mean {row['mean_us']:>9}  p50 {row['p50_us']:>9}  "
          f"p95 {row['p95_us']:>9}  p99 {row['p99_us']:>9}   qps {row['qps']}")
    print(f"  S  shards touched          set {a.shards:<4} measured {row['actual_shards_touched']}")
    print(f"  N  QueryNodes PER SHARD     pods {a.querynode_pods:<3} measured {row['actual_N_per_shard_avg']}"
          f"   (control plane min/avg/max {row['cp_N_per_shard_min']}/{row['cp_N_per_shard_avg']}/{row['cp_N_per_shard_max']})")
    print(f"  M  segments PER QUERYNODE   chunks {a.insert_chunks:<3} measured {row['actual_M_per_worker_per_shard']}")
    print(f"     secondary: N_unique_total={row['actual_N_unique_total']}  "
          f"M_total_searched={row['actual_M_total_searched']}  "
          f"worker RPCs/query={row['actual_worker_rpcs_per_query']}  "
          f"(growing on delegator: {row['actual_growing_segments_per_query']})")
    print(f"  net/query    proxy {row['rx_kb_per_query_proxy']}/{row['tx_kb_per_query_proxy']} KB  "
          f"stream {row['rx_kb_per_query_streaming']}/{row['tx_kb_per_query_streaming']} KB  "
          f"query {row['rx_kb_per_query_query']}/{row['tx_kb_per_query_query']} KB  "
          f"snqn {row['eth0_rx_kb_per_query_snqn']}/{row['eth0_tx_kb_per_query_snqn']} KB  (eth0 rx/tx)")
    print(f"  lo/query     total {row['lo_kb_per_query_total']} KB  (snqn {row['lo_kb_per_query_snqn']})"
          f"   sum-of-pod-eth0 rx+tx {row['sum_pod_eth0_rxtx_kb_per_query']} KB (not physical)")
    if underflow:
        print(f"  !! idle-baseline underflow on {underflow} -- re-run 04_baseline_idle.sh")
    if baseline_bad:
        print(f"  !! idle baseline accounts for >50% of the measured bytes on {baseline_bad}.")
        print(f"     The baseline was probably taken before the cluster settled; the network")
        print(f"     columns of this row are NOT usable. Re-take it and re-run.")
    if a.recall:
        print(f"  recall@{a.topk}   {row['recall_at_k']}")
    if errors:
        print(f"  !! {errors} failed queries")
    print(f"\n  raw  -> {raw_csv}\n  json -> {run_json}\n  row  -> {runs_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
