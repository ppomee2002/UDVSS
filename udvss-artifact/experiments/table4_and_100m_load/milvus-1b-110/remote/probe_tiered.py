#!/usr/bin/env python3
"""Tiered-storage probe: after load, time ef=64 searches (nq=1 x 5, then one nq=1024 batch,
the protocol's request unit) and report recall@10 on what returned."""
import sys, time, json, os
sys.path.insert(0, "scripts"); sys.path.insert(0, "/home/dev_path/milvus-motivation/scripts")
import bench_env as E
from lib.dataset import open_dataset, read_fvecs, read_ivecs, recall_at_k
from pymilvus import MilvusClient
import numpy as np
ds = open_dataset("deep1b1b", E.DATASET_DIR); q = read_fvecs(ds.query_path); gt = read_ivecs(ds.gt_path)
c = MilvusClient(uri=E.URI, timeout=7200); name = E.collection_name("deep1b1b")
sp = {"params": {"ef": 64}}
for i in range(5):
    t = time.time(); r = c.search(name, data=[q[i].tolist()], limit=10, search_params=sp)
    ids = [h["id"] for h in r[0]]
    print(f"nq=1 #{i}: {time.time()-t:.1f}s recall={len(set(ids)&set(gt[i][:10].tolist()))/10}", flush=True)
t = time.time(); r = c.search(name, data=q[:1000].tolist() + q[:24].tolist(), limit=10, search_params=sp)
el = time.time() - t
ids = np.array([[h["id"] for h in x] for x in r[:1000]])
print(json.dumps(dict(nq=1024, seconds=round(el, 1), qps=round(1024 / el, 2),
                      recall_at_10=recall_at_k(ids, gt[:1000], 10))), flush=True)
