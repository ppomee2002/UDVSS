#!/usr/bin/env python3
"""Step F. Prove that S, N and M are controllable AND observable, before spending
time on the full matrix.

For a loaded collection it prints, side by side:

  control plane  (what Milvus SAYS it will do)
      describe_collection.num_shards
      get_replicas -> per replica, per shard: channel, leaderID (delegator), node_ids
      get_query_segment_info -> segment -> nodeIds, state, num_rows
  data plane     (what a burst of real queries ACTUALLY did)
      S from delegator shard-reduce counter deltas
      N from worker SearchSegments counter deltas
      M from per-segment search histogram counts

A mismatch between the two columns is the interesting result, not an error --
it is exactly what the "do not equate parameters with reality" requirement is about.

    07_probe_snm.py --collection mm_deep1m_s4_c8 --queries 200
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from pymilvus import MilvusClient, connections, utility

from lib.common import list_pods, pods_by_component, write_json
from lib.dataset import open_dataset, read_fvecs
from lib import milvus_control as mc
from lib import milvus_metrics as mm


def control_plane(client: MilvusClient, coll: str) -> dict:
    """What Milvus DECLARES. Every number here is an upper bound on participation:
    QueryCoord lists a node for a shard even when the delegator ends up skipping it
    for holding zero segments (`organizeSubTask(..., skipEmpty=True)`)."""
    desc = client.describe_collection(coll)
    out = {"num_shards": desc.get("num_shards"), "collection_id": desc.get("collection_id")}
    out.update(mc.shard_topology(coll, client=client))
    out.update(mc.segment_placement(coll))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default=os.environ.get("MM_URI"))
    ap.add_argument("--collection", required=True)
    ap.add_argument("--dataset", default=os.environ.get("MM_DATASET", "deep1m"))
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=50)
    ap.add_argument("--topk", type=int, default=int(os.environ.get("MM_TOPK", 10)))
    ap.add_argument("--search-params", default=os.environ.get("MM_SEARCH_PARAMS", '{"ef":64}'))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if not a.uri:
        print("set --uri or MM_URI", file=sys.stderr)
        return 2

    client = MilvusClient(uri=a.uri)
    connections.connect(alias="mm", uri=a.uri)

    print("=" * 78)
    print("CONTROL PLANE")
    print("=" * 78)
    cp = control_plane(client, a.collection)
    print(json.dumps(cp, indent=2))

    ds = open_dataset(a.dataset)
    qv = read_fvecs(ds.query_path, 0, a.warmup + a.queries)
    sp = json.loads(a.search_params)

    pods = list_pods()
    bycomp = pods_by_component(pods)
    print()
    print("pods:", {k: [p.node for p in v] for k, v in bycomp.items()})

    print(f"\nwarmup {a.warmup} queries (NQ=1) ...")
    for i in range(a.warmup):
        client.search(a.collection, data=[qv[i].tolist()], limit=a.topk,
                      search_params=sp, anns_field="vector")

    print(f"measuring {a.queries} queries (NQ=1, concurrency 1) ...")
    before = mm.Snapshot.take(pods)
    t0 = time.perf_counter()
    for i in range(a.queries):
        client.search(a.collection, data=[qv[a.warmup + i].tolist()], limit=a.topk,
                      search_params=sp, anns_field="vector")
    wall = time.perf_counter() - t0
    after = mm.Snapshot.take(pods)

    snm = mm.measure_snm(before, after, a.queries)

    print()
    print("=" * 78)
    print("DATA PLANE (measured over %d queries, %.2fs)" % (a.queries, wall))
    print("=" * 78)
    print(f"S  shards touched per query        {snm.S_actual:.4f}")
    print(f"   ({snm.shard_reduces_total:.0f} shard-level reduces over {a.queries} queries)")
    for p, v in sorted(snm.shard_reduces_by_pod.items()):
        print(f"      {p:<46} {v/a.queries:.4f}/query   (node {after.pods[p].node})")
    print()
    print(f"N  QueryNodes PER SHARD (primary)    {snm.N_per_shard_avg:.4f}")
    print(f"   = worker RPCs/query ({snm.worker_rpcs_per_query:.4f}) / S ({snm.S_actual:.4f})")
    print(f"   N_unique_total (secondary)        {snm.N_unique_total}  "
          f"<- distinct QueryNode pods over the whole query, NOT N")
    for p, v in sorted(snm.worker_rpcs_by_pod.items()):
        cov = snm.shards_served_by_pod.get(p)
        cs = f"   serves {cov:.2f} of the {snm.S_actual:.0f} shards" if cov else ""
        print(f"      {p:<46} {v/a.queries:.4f} RPC/query{cs}")
    print()
    print(f"M  segments PER QUERYNODE PER SHARD  {snm.M_per_worker_per_shard:.4f}   (primary)")
    print(f"   = sealed searches / worker RPCs")
    print(f"   M_total_searched (secondary)      {snm.M_total_searched:.4f} per query "
          f"(sealed {snm.M_sealed_per_query:.4f} + growing {snm.M_growing_per_query:.4f})")
    for p, v in sorted(snm.M_per_worker_per_shard_by_pod.items()):
        print(f"      {p:<46} {v:.4f} segments per SearchSegments RPC")
    if snm.M_growing_per_query:
        print(f"   growing segments are searched on the DELEGATOR, not on a QueryNode, so they")
        print(f"   are excluded from M. Flush before measuring to drive this to 0.")

    print()
    print("=" * 78)
    print("CONTROL vs DATA")
    print("=" * 78)
    verdicts = []

    def check(label, expected, actual, tol=0.02):
        ok = expected is not None and abs(float(actual) - float(expected)) <= tol
        verdicts.append((label, expected, actual, ok))
        print(f"  {'OK  ' if ok else 'DIFF'} {label:<44} declared={expected}  measured={actual}")

    check("S: num_shards vs shards touched/query", cp["num_shards"], round(snm.S_actual, 4))
    check("N per shard: declared vs measured",
          cp.get("cp_N_per_shard_avg"), round(snm.N_per_shard_avg, 4), tol=0.05)
    check("N_unique_total: nodes with segments vs pods hit",
          cp.get("nodes_with_segments"), snm.N_unique_total, tol=0.0)
    check("M per QueryNode: declared segs/node vs measured",
          cp.get("cp_segments_per_node_avg"),
          round(snm.M_per_worker_per_shard * max(snm.S_actual, 1.0), 4), tol=0.05)
    check("M_total_searched: collection segments vs searched/query",
          cp.get("segments_total"), round(snm.M_total_searched, 4))

    cpmin, cpmax = cp.get("cp_N_per_shard_min"), cp.get("cp_N_per_shard_max")
    if cpmin != "" and cpmax != "":
        print(f"  ---- N per shard, control-plane spread: min={cpmin} max={cpmax} "
              f"({cp.get('cp_shard_nodes','')})")
        print(f"       Prometheus counters carry no channel label, so a MEASURED per-shard")
        print(f"       min/max is not obtainable; the measured N is the average over shards.")
        print(f"       When min == max the average is exact for every shard.")

    print()
    print("Definitions in use (the supervisor's):")
    print("  S = shards a query touches")
    print("  N = QueryNodes that participate PER SHARD      = worker RPCs / S")
    print("  M = segments searched PER QUERYNODE PER SHARD  = sealed searches / worker RPCs")
    print("N_unique_total and M_total_searched are whole-query aggregates and are reported")
    print("separately; they are NOT N and M. 'M_total_searched vs collection segments'")
    print("coincide only when every segment of every shard is visited -- the no-pruning case,")
    print("which is the expected result for a plain ANN search in 2.6.23. The point of")
    print("measuring is that it is now shown rather than assumed.")

    out = a.out or os.path.join(os.environ.get("MM_RESULTS", "."), "probe",
                                f"{a.collection}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    write_json(out, {
        "collection": a.collection,
        "queries": a.queries,
        "wall_seconds": wall,
        "control_plane": cp,
        "data_plane": {
            "S_actual": snm.S_actual,
            "N_per_shard_avg": snm.N_per_shard_avg,
            "M_per_worker_per_shard": snm.M_per_worker_per_shard,
            "N_unique_total": snm.N_unique_total,
            "M_total_searched": snm.M_total_searched,
            "worker_rpcs_per_query": snm.worker_rpcs_per_query,
            "M_sealed_per_query": snm.M_sealed_per_query,
            "M_growing_per_query": snm.M_growing_per_query,
            "shard_reduces_by_pod": snm.shard_reduces_by_pod,
            "worker_rpcs_by_pod": snm.worker_rpcs_by_pod,
            "sealed_by_pod": snm.sealed_segment_searches_by_pod,
            "growing_by_pod": snm.growing_segment_searches_by_pod,
            "M_per_worker_per_shard_by_pod": snm.M_per_worker_per_shard_by_pod,
            "shards_served_by_pod": snm.shards_served_by_pod,
        },
        "pods": {p.name: {"component": p.component, "node": p.node} for p in pods},
        "verdicts": [{"label": l, "declared": e, "measured": m, "match": ok} for l, e, m, ok in verdicts],
    })
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
