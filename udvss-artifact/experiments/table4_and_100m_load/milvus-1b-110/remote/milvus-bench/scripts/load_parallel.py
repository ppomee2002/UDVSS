#!/usr/bin/env python3
"""Deep1B 1B on 110 (user 2026-10-28): load_dataset.do_load with the insert split over
P processes (the load may use more cores; only QPS must match UDVSS). Collection,
schema, HNSW M=30/efConstruction=360, shard 1, replica 1 and the load step are the
same as load_dataset.py; only the insert is parallel.

    load_parallel.py --dataset deep1b1b --procs 24 [--skip-insert] [--load-only]
"""
import argparse, json, multiprocessing as mp, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/home/dev_path/milvus-motivation/scripts")
from lib.dataset import open_dataset, read_fvecs
import bench_env as E
from load_dataset import disk_line
from pymilvus import MilvusClient, DataType

CHUNK = 10_000

def worker(ds, lo, hi, wid, q):
    d = open_dataset(ds, E.DATASET_DIR); name = E.collection_name(ds)
    c = MilvusClient(uri=E.URI)
    for i in range(lo, hi, CHUNK):
        ch = read_fvecs(d.base_path, i, min(CHUNK, hi - i))
        for t in range(240):                       # retry transient errors (e.g. rate limit)
            try:
                c.insert(name, [{"id": int(i + j), "vector": ch[j].tolist()} for j in range(len(ch))])
                break
            except Exception as e:                 # noqa: BLE001
                print(f"  w{wid} insert {i} retry {t}: {str(e)[:200]}", flush=True)
                time.sleep(30)
        else:
            raise RuntimeError(f"w{wid}: insert at {i} failed 240 times")
        q.put(len(ch))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--procs", type=int, default=24)
    ap.add_argument("--skip-insert", action="store_true")
    ap.add_argument("--load-only", action="store_true")
    a = ap.parse_args()
    ds = a.dataset; d = open_dataset(ds, E.DATASET_DIR); name = E.collection_name(ds)
    c = MilvusClient(uri=E.URI)
    os.makedirs(E.RESULTS, exist_ok=True)
    bj = f"{E.RESULTS}/{ds}_build.json"
    info = json.load(open(bj)) if os.path.exists(bj) else dict(
        dataset=ds, count=d.count, dim=d.dim, metric=d.metric, index_type=E.INDEX_TYPE,
        index_params=E.INDEX_PARAMS, num_shards=E.NUM_SHARDS,
        replica_number=E.REPLICA_NUMBER, insert_procs=a.procs)
    if not (a.skip_insert or a.load_only):
        print(f"{ds}: {d.count} x {d.dim} {d.metric} -> collection {name}, {a.procs} procs")
        disk_line("before_load", ds)
        if c.has_collection(name):
            print("  already exists, dropping first"); c.drop_collection(name)
        s = c.create_schema(auto_id=False, enable_dynamic_field=False)
        s.add_field("id", DataType.INT64, is_primary=True)
        s.add_field("vector", DataType.FLOAT_VECTOR, dim=d.dim)
        c.create_collection(name, schema=s, num_shards=E.NUM_SHARDS)
        t0 = time.time(); q = mp.Queue()
        step = -(-d.count // a.procs // CHUNK) * CHUNK
        ps = [mp.Process(target=worker, args=(ds, lo, min(lo + step, d.count), w, q))
              for w, lo in enumerate(range(0, d.count, step))]
        for p in ps: p.start()
        n, nxt = 0, 10_000_000
        while n < d.count:
            try:
                n += q.get(timeout=60)
            except Exception:                      # noqa: BLE001
                if not any(p.is_alive() for p in ps): break
                continue
            if n >= nxt:
                el = time.time() - t0
                print(f"  inserted {n:>11}/{d.count}  {el:8.1f}s  {n / el:7.0f}/s", flush=True)
                nxt += 10_000_000
        for p in ps: p.join()
        assert n == d.count and all(p.exitcode == 0 for p in ps), f"{n} != {d.count} or a worker failed"
        c.flush(name)
        info["insert_seconds"] = round(time.time() - t0, 1)
        print(f"  insert+flush {info['insert_seconds']}s")
        disk_line("after_insert", ds)
        json.dump(info, open(bj, "w"), indent=2)
    if not a.load_only:
        t0 = time.time()
        ip = c.prepare_index_params()
        ip.add_index(field_name="vector", index_type=E.INDEX_TYPE, metric_type=E.METRIC,
                     params=dict(E.INDEX_PARAMS))
        c.create_index(name, ip)
        last = 0
        while True:
            st = c.describe_index(name, "vector")
            if st.get("state") == "Finished":
                break
            if st.get("state") == "Failed":
                raise RuntimeError(f"index build failed: {st}")
            if time.time() - last > 600:
                print(f"  index {st.get('indexed_rows')}/{st.get('total_rows')} "
                      f"{time.time()-t0:.0f}s state={st.get('state')}", flush=True)
                disk_line("index_progress", ds); last = time.time()
            time.sleep(10)
        info["index_seconds"] = round(time.time() - t0, 1)
        print(f"  index build {info['index_seconds']}s  ({E.INDEX_TYPE} {E.INDEX_PARAMS})")
        disk_line("after_index", ds)
        json.dump(info, open(bj, "w"), indent=2)
    t0 = time.time()
    c.load_collection(name, replica_number=E.REPLICA_NUMBER)
    info["load_seconds"] = round(time.time() - t0, 1)
    print(f"  load {info['load_seconds']}s  rows={c.get_collection_stats(name)['row_count']}")
    disk_line("after_load", ds)
    json.dump(info, open(bj, "w"), indent=2)

if __name__ == "__main__":
    main()
