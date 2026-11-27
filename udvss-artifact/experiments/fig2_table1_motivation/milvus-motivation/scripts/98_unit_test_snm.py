"""Unit-test measure_snm against a hand-built scenario: S=4, N=2 per shard, M=3 per worker."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.common import Pod
from lib.milvus_metrics import Snapshot, measure_snm

S, N, M, Q = 4, 2, 3, 100
pods = {
    "sn-0": Pod("sn-0", "streamingnode", "stream110", "10.42.1.1"),
    "qn-0": Pod("qn-0", "querynode", "query109", "10.42.2.1"),
    "qn-1": Pod("qn-1", "querynode", "query109", "10.42.2.2"),
}
SEARCH = (("query_type", "search"),)

def key(name, **lbl):
    return (name, tuple(sorted(list(SEARCH) + [(k, v) for k, v in lbl.items()])))

zero = {p: {} for p in pods}
before = Snapshot(zero, pods)

after_data = {
    # delegator: one shard-reduce per inbound Search RPC -> S per query
    "sn-0": {key("milvus_querynode_sq_reduce_latency_count",
                 reduce_level="shards", reduce_type="batch_reduce"): S * Q},
    # each worker gets one SearchSegments RPC per shard -> S per query per worker
    "qn-0": {key("milvus_querynode_sq_req_count", scope="FromLeader", status="Success"): S * Q,
             key("milvus_querynode_sq_segment_latency_count", segment_state="Sealed"): S * M * Q},
    "qn-1": {key("milvus_querynode_sq_req_count", scope="FromLeader", status="Success"): S * Q,
             key("milvus_querynode_sq_segment_latency_count", segment_state="Sealed"): S * M * Q},
}
after = Snapshot(after_data, pods)
r = measure_snm(before, after, Q)

checks = [
    ("S_actual",                S,      r.S_actual),
    ("N_per_shard_avg",         N,      r.N_per_shard_avg),
    ("M_per_worker_per_shard",  M,      r.M_per_worker_per_shard),
    ("N_unique_total",          N,      r.N_unique_total),
    ("M_total_searched",        S*N*M,  r.M_total_searched),
    ("worker_rpcs_per_query",   S*N,    r.worker_rpcs_per_query),
    ("shards_served qn-0",      1.0,    r.shards_served_by_pod["qn-0"]),
]
bad = 0
for name, want, got in checks:
    good = abs(float(got) - float(want)) < 1e-9
    print(f"  {'OK  ' if good else 'FAIL'} {name:<24} want {want:<8} got {got}")
    bad += not good

# asymmetric case: qn-1 serves only 2 of the 4 shards, with 5 segments per RPC
after_data["qn-1"] = {
    key("milvus_querynode_sq_req_count", scope="FromLeader", status="Success"): 2 * Q,
    key("milvus_querynode_sq_segment_latency_count", segment_state="Sealed"): 2 * 5 * Q,
}
r2 = measure_snm(before, Snapshot(after_data, pods), Q)
exp_N = (S + 2) / S                       # (4 + 2) RPCs per query / 4 shards = 1.5
exp_M = (S*M + 2*5) / (S + 2)             # (12 + 10) segs / 6 RPCs = 3.667
print("  -- asymmetric spread (qn-1 serves 2 of 4 shards, 5 segs/RPC)")
for name, want, got in [("N_per_shard_avg", exp_N, r2.N_per_shard_avg),
                        ("M_per_worker_per_shard", exp_M, r2.M_per_worker_per_shard),
                        ("M per pod qn-0", M, r2.M_per_worker_per_shard_by_pod["qn-0"]),
                        ("M per pod qn-1", 5.0, r2.M_per_worker_per_shard_by_pod["qn-1"]),
                        ("shards_served qn-1", 0.5, r2.shards_served_by_pod["qn-1"])]:
    good = abs(float(got) - float(want)) < 1e-9
    print(f"  {'OK  ' if good else 'FAIL'} {name:<24} want {want:<8.4f} got {got}")
    bad += not good

row = r2.as_row()
print("  -- as_row() primary columns")
for k in ("actual_shards_touched", "actual_N_per_shard_avg", "actual_M_per_worker_per_shard",
          "actual_N_unique_total", "actual_M_total_searched",
          "actual_shards_served_min", "actual_shards_served_max"):
    print(f"       {k:<38} {row[k]}")
sys.exit(1 if bad else 0)
