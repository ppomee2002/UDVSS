#!/usr/bin/env python3
"""engine_qdrant.load with the upsert split over P processes (user 2026-10-28: load as
fast as possible; QPS conditions unchanged). Collection config is engine_qdrant.load's
verbatim (create step copied); same UPSERT_CHUNK, wait=False, gRPC; then E.wait_indexed."""
import sys, json, time, multiprocessing as mp
sys.path.insert(0, '/home/dev_path/corun/scripts')
import engine_qdrant as E
from engine_qdrant import models, open_dataset, read_fvecs, C

def worker(ds, lo, hi, q):
    d = open_dataset(ds, C.DATASET_DIR); c, name = E.client(), E.collection(ds)
    for i in range(lo, hi, E.UPSERT_CHUNK):
        ch = read_fvecs(d.base_path, i, min(E.UPSERT_CHUNK, hi - i))
        for t in range(240):
            try:
                c.upsert(collection_name=name, wait=False, points=models.Batch(
                    ids=list(range(i, i + len(ch))), vectors=ch.tolist())); break
            except Exception as e:  # noqa: BLE001
                print(f"  upsert {i} retry {t}: {str(e)[:200]}", flush=True); time.sleep(30)
        else:
            raise RuntimeError(f"upsert at {i} failed 240 times")
        q.put(len(ch))

def main(ds, procs):
    d = open_dataset(ds, C.DATASET_DIR); c, name = E.client(), E.collection(ds)
    print(f"{ds}: {d.count} x {d.dim} L2 -> {name}, {procs} upsert procs", flush=True)
    if c.collection_exists(name):
        print("  already exists, dropping first"); c.delete_collection(name)
    c.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(size=d.dim, distance=models.Distance.EUCLID,
                                           on_disk=E.VEC_ON_DISK),
        hnsw_config=models.HnswConfigDiff(m=C.HNSW_M, ef_construct=C.HNSW_EFC,
                                          on_disk=E.HNSW_ON_DISK, full_scan_threshold=10),
        optimizers_config=models.OptimizersConfigDiff(indexing_threshold=1),
        shard_number=C.SHARDS, replication_factor=C.REPLICAS, on_disk_payload=False)
    print(f"  config: vec on_disk={E.VEC_ON_DISK} hnsw on_disk={E.HNSW_ON_DISK} "
          f"m={C.HNSW_M} efc={C.HNSW_EFC} shards={C.SHARDS}", flush=True)
    t0 = time.time(); q = mp.Queue(); ch = E.UPSERT_CHUNK
    step = -(-d.count // procs // ch) * ch
    ps = [mp.Process(target=worker, args=(ds, lo, min(lo + step, d.count), q))
          for lo in range(0, d.count, step)]
    for p in ps: p.start()
    n, nxt = 0, 10_000_000
    while n < d.count:
        try:
            n += q.get(timeout=60)
        except Exception:  # noqa: BLE001
            if not any(p.is_alive() for p in ps): break
            continue
        if n >= nxt:
            el = time.time() - t0
            print(f"  upserted {n:>11}/{d.count}  {el:8.1f}s  {n / el:6.0f}/s", flush=True); nxt += 10_000_000
    for p in ps: p.join()
    assert n == d.count and all(p.exitcode == 0 for p in ps), f"{n} != {d.count} or worker failed"
    t_feed = time.time() - t0; print(f"  feed {t_feed:.1f}s", flush=True)
    t0 = time.time(); ok, info = E.wait_indexed(ds, d.count)
    if not ok:
        raise RuntimeError(f"{ds}: Qdrant never reached a settled indexed state")
    print(json.dumps(dict(feed_seconds=round(t_feed, 1), index_seconds=round(time.time() - t0, 1),
                          upsert_procs=procs, **info), default=str))

if __name__ == '__main__':
    main(sys.argv[1], int(sys.argv[2]))
