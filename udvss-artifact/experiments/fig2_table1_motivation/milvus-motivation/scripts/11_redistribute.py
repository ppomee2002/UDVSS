#!/usr/bin/env python3
"""Redistribute loaded collections over the CURRENT QueryNode pod set.

Why this exists: `queryCoord.autoBalance` is deliberately false (docs/01_findings.md F7)
so that N and M stay frozen during a measurement. The side effect is that scaling
`queryNode.replicas` does NOT move any segment -- a freshly added pod receives nothing
and the measured N stays where it was. Verified empirically: with 2 QueryNode pods
Running, `actual_N_per_shard_avg` was still 1.0 and the new pod saw zero traffic.

Release + load re-runs the initial assignment, which is not the periodic balancer and
does spread over whatever pods exist at that moment.

    11_redistribute.py                 # every loaded collection
    11_redistribute.py --collections a b
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pymilvus import MilvusClient


def redistribute(c: MilvusClient, name: str, replica_number: int, timeout: float = 300.0) -> dict:
    c.release_collection(name)
    time.sleep(2)
    # A QueryNode pod that is Running is not yet usable: QueryCoord only accepts a load
    # once the node has registered into the resource group. Right after a scale-up the
    # load fails with
    #   code=304 ... resource group node not enough[rg=__default_resource_group][currentNodeNum=0]
    # which leaves the collection RELEASED. Retry instead of giving up.
    last = None
    for attempt in range(30):
        try:
            c.load_collection(name, replica_number=replica_number)
            break
        except Exception as exc:
            last = exc
            if "resource group node not enough" not in str(exc):
                raise
            if attempt == 0:
                print(f"    {name}: waiting for QueryNodes to register with QueryCoord ...")
            time.sleep(5)
    else:
        raise RuntimeError(f"{name}: QueryNodes never registered: {last}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if str(c.get_load_state(name).get("state")).endswith("Loaded"):
            break
        time.sleep(2)
    else:
        raise TimeoutError(f"{name} did not finish loading in {timeout}s")

    reps = c.describe_replica(name)
    out = {"collection": name, "replicas": []}
    for r in reps:
        shards = [{"channel": s.channel_name, "leader": s.shard_leader,
                   "nodes": sorted(int(x) for x in s.shard_nodes)} for s in r.shards]
        counts = [len(s["nodes"]) for s in shards]
        out["replicas"].append({
            "id": r.id,
            "group_nodes": sorted(int(x) for x in r.group_nodes),
            "n_shards": len(shards),
            "cp_N_per_shard_min": min(counts) if counts else None,
            "cp_N_per_shard_max": max(counts) if counts else None,
            "shards": shards,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default=os.environ.get("MM_URI"))
    ap.add_argument("--collections", nargs="*", default=None)
    ap.add_argument("--replica-number", type=int, default=1)
    ap.add_argument("--prefix", default="mm_")
    a = ap.parse_args()
    if not a.uri:
        print("set --uri or MM_URI", file=sys.stderr)
        return 2

    c = MilvusClient(uri=a.uri, timeout=120)
    names = a.collections or [n for n in c.list_collections() if n.startswith(a.prefix)]
    if not names:
        print("no collections to redistribute")
        return 1

    print(f"redistributing {len(names)} collection(s) over the current QueryNode pod set")
    bad = 0
    for n in sorted(names):
        try:
            r = redistribute(c, n, a.replica_number)
        except Exception as exc:
            print(f"  FAIL {n}: {type(exc).__name__}: {exc}")
            bad += 1
            continue
        for rep in r["replicas"]:
            print(f"  {n:<26} nodes={rep['group_nodes']}  shards={rep['n_shards']}  "
                  f"N/shard min={rep['cp_N_per_shard_min']} max={rep['cp_N_per_shard_max']}")
    print("\nNOTE: these are CONTROL-PLANE numbers (an upper bound). Confirm the measured N")
    print("      with 07_probe_snm.py before trusting any row that depends on it.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
