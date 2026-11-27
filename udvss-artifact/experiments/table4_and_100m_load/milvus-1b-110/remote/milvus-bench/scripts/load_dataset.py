#!/usr/bin/env python3
"""Bring one dataset up in the standalone Milvus, or take it back down.

    load_dataset.py --dataset nytimes            # create, insert, index, load
    load_dataset.py --dataset nytimes --drop     # drop and wait for the space back

Only one dataset is ever resident: /home/ubuntu is shared with everything else
on the host, so each dataset is dropped before the next is created and the space
is confirmed returned.  Reclamation is asynchronous -- the compose file lowers
dataCoord.gc.* to make it minutes rather than hours -- and it never returns to
zero because the woodpecker WAL (a-bucket/files/wp) stays behind, so the check
is "the collection prefixes are gone and the size has settled", not "size is 0".
"""
import argparse, json, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/home/dev_path/milvus-motivation/scripts")
from lib.dataset import open_dataset, read_fvecs
import bench_env as E
from pymilvus import MilvusClient, DataType

CHUNK = 10_000

def disk_line(tag, ds):
    row = dict(utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), phase=tag, dataset=ds,
               free_gb=round(E.host_df_gb(), 2), minio_gb=round(E.host_volume_gb("minio"), 3),
               milvus_gb=round(E.host_volume_gb("milvus"), 3))
    os.makedirs(f"{E.ROOT}/logs", exist_ok=True)
    with open(f"{E.ROOT}/logs/disk.jsonl", "a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(f"  [disk] {tag:<12} free={row['free_gb']:.1f}G minio={row['minio_gb']:.3f}G "
          f"milvus={row['milvus_gb']:.3f}G")
    return row

def prefixes():
    """Which Milvus prefixes still exist under the bucket."""
    out = E.host(f"ls {E.HOST_VOLUMES}/minio/a-bucket/files 2>/dev/null")
    return sorted(out.split()) if out else []

def do_drop(c, ds, wait_s=900):
    name = E.collection_name(ds)
    if c.has_collection(name):
        c.release_collection(name)
        c.drop_collection(name)
        print(f"dropped {name}")
    disk_line("after_drop", ds)
    print(f"waiting for GC (up to {wait_s}s); prefixes now: {prefixes()}")
    stable, last = 0, None
    for i in range(wait_s // 30):
        time.sleep(30)
        size = E.host_volume_gb("minio"); pre = prefixes()
        dirty = [p for p in pre if p in ("insert_log", "index_files", "delta_log", "stats_log")]
        print(f"  +{(i+1)*30:4d}s minio={size:.3f}G prefixes={pre}")
        if not dirty:
            if last is not None and abs(size - last) < 0.01:
                stable += 1
                if stable >= 2:
                    disk_line("gc_settled", ds)
                    print(f"OK: collection data gone, size settled at {size:.3f}G "
                          f"(residual is the woodpecker WAL)")
                    return True
            else:
                stable = 0
        last = size
    disk_line("gc_TIMEOUT", ds)
    print(f"FAIL: still holding {prefixes()} after {wait_s}s -- not continuing", file=sys.stderr)
    return False

def do_load(c, ds):
    d = open_dataset(ds, E.DATASET_DIR)
    name = E.collection_name(ds)
    print(f"{ds}: {d.count} x {d.dim} {d.metric} -> collection {name}")
    disk_line("before_load", ds)
    if c.has_collection(name):
        print("  already exists, dropping first"); c.drop_collection(name)

    s = c.create_schema(auto_id=False, enable_dynamic_field=False)
    s.add_field("id", DataType.INT64, is_primary=True)
    s.add_field("vector", DataType.FLOAT_VECTOR, dim=d.dim)
    c.create_collection(name, schema=s, num_shards=E.NUM_SHARDS)

    # Read the base CHUNK rows at a time: deep1b100m is 38 GB, and holding it all in
    # this process while Milvus builds and loads the index would starve the host.
    t0 = time.time(); n = 0
    for i in range(0, d.count, CHUNK):
        ch = read_fvecs(d.base_path, i, min(CHUNK, d.count - i))
        c.insert(name, [{"id": int(i + j), "vector": ch[j].tolist()} for j in range(len(ch))])
        n += len(ch)
        if (i // CHUNK) % 20 == 0:
            print(f"  inserted {n:>9}/{d.count}  {time.time()-t0:6.1f}s", flush=True)
    assert n == d.count, f"{n} != {d.count}"
    c.flush(name)
    t_insert = time.time() - t0
    print(f"  insert+flush {t_insert:.1f}s")
    disk_line("after_insert", ds)

    t0 = time.time()
    ip = c.prepare_index_params()
    ip.add_index(field_name="vector", index_type=E.INDEX_TYPE, metric_type=E.METRIC,
                 params=dict(E.INDEX_PARAMS))
    c.create_index(name, ip)
    while True:
        st = c.describe_index(name, "vector")
        if st.get("state") == "Finished":
            break
        time.sleep(5)
    t_index = time.time() - t0
    print(f"  index build {t_index:.1f}s  ({E.INDEX_TYPE} {E.INDEX_PARAMS})")
    disk_line("after_index", ds)

    t0 = time.time()
    c.load_collection(name, replica_number=E.REPLICA_NUMBER)
    t_load = time.time() - t0
    print(f"  load {t_load:.1f}s  rows={c.get_collection_stats(name)['row_count']}")
    disk_line("after_load", ds)
    with open(f"{E.RESULTS}/{ds}_build.json", "w") as fh:
        json.dump(dict(dataset=ds, count=d.count, dim=d.dim, metric=d.metric,
                       index_type=E.INDEX_TYPE, index_params=E.INDEX_PARAMS,
                       num_shards=E.NUM_SHARDS, replica_number=E.REPLICA_NUMBER,
                       insert_seconds=round(t_insert, 1), index_seconds=round(t_index, 1),
                       load_seconds=round(t_load, 1)), fh, indent=2)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--drop", action="store_true")
    a = ap.parse_args()
    os.makedirs(f"{E.RESULTS}", exist_ok=True)
    cli = MilvusClient(uri=E.URI)
    sys.exit(0 if (do_drop(cli, a.dataset) if a.drop else (do_load(cli, a.dataset) or True)) else 1)
