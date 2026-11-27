#!/usr/bin/env python3
"""Side sampler: every 30 s, per arm, decompose seastar memory from `dump_metrics`:
allocated = cache LRU + dirty extents + rest; plus live allocations and per-tree extent
counts. Read-only (admin command), independent of run_arm.py."""
import json, os, subprocess, sys, time, collections
BUILD = "/home/dev_path/ceph-vectornode-seastore-batch/build"
OUT = os.environ.get("MB_OUT", "/home/dev_path/experiments/memfootprint-sq8/ceph/results/prof12/mem_breakdown.jsonl")
env = {"LD_LIBRARY_PATH": f"{BUILD}/lib", "PATH": "/usr/bin:/bin"}
while True:
    for a in ("fp32", "int8"):
        conf = f"/home/dev_path/vstart/memfp2-{a}/ceph.conf"
        try:
            r = subprocess.run([f"{BUILD}/bin/ceph", "-c", conf, "tell", "osd.0", "dump_metrics"],
                               capture_output=True, text=True, timeout=60, env=env)
            j = json.loads(r.stdout)
        except Exception:
            continue
        tot = collections.defaultdict(float); trees = collections.defaultdict(float)
        for m in j.get("metrics", []):
            for k, v in m.items():
                if not isinstance(v, dict) or not isinstance(v.get("value"), (int, float)):
                    continue
                tot[k] += v["value"]
                if k == "cache_tree_extents_num":
                    trees[v.get("tree", "?")] += v["value"]
        G = 1024 ** 3
        row = {"t": time.time(), "arm": a,
               "alloc_gb": tot["memory_allocated_memory"] / G,
               "lru_gb": tot["cache_lru_size_bytes"] / G,
               "dirty_gb": tot["cache_dirty_extent_bytes"] / G,
               "rest_gb": (tot["memory_allocated_memory"] - tot["cache_lru_size_bytes"]
                           - tot["cache_dirty_extent_bytes"]) / G,
               "live_objects": tot["memory_malloc_live_objects"],
               "lru_extents": tot["cache_lru_num_extents"],
               "trees": dict(trees)}
        with open(OUT, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    time.sleep(30)
