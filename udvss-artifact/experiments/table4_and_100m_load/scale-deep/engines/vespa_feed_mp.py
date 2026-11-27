"""Parallel Vespa feed for deep1b100m: the single-process feeder in engine_vespa.load()
is GIL-bound at ~1.3k docs/s (Vespa ~2 cores busy) -> ~21 h for 100M. Same documents
(id i -> base row i, idempotent PUT), same deployed schema/HNSW params; only the
client is split over processes. Usage: vespa_feed_mp.py <dataset> <procs> <threads> [start]
507 (feed blocked: resource limit, e.g. a transient flush spike on disk) is retried for up to 2 h."""
import os, sys, time
from multiprocessing import Process, Queue
sys.path.insert(0, "/home/dev_path/corun/scripts")
import common as C, engine_vespa as E
from lib.dataset import open_dataset, read_fvecs
from concurrent.futures import ThreadPoolExecutor
CH = 20_000
import requests as _rq

def put507(i, vec):
    for _ in range(240):
        e = E._put(i, vec)
        if e is None or " 507 " not in e: return e
        time.sleep(30)
    return e

def worker(w, P, T, path, count, q, start):
    errs = 0
    with ThreadPoolExecutor(max_workers=T) as ex:
        for off in range(start + w * CH, count, P * CH):
            blk = read_fvecs(path, off, min(CH, count - off)).tolist()
            for e in ex.map(lambda t: put507(t[0], t[1]),
                            ((off + j, blk[j]) for j in range(len(blk)))):
                if e:
                    errs += 1
                    if errs <= 3: print(f"  feed error w{w}: {e}", flush=True)
            q.put((len(blk), 0))
    q.put((0, errs)); q.put(None)

if __name__ == "__main__":
    ds, P, T = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    start = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    d = open_dataset(ds, C.DATASET_DIR)
    print(f"{ds}: parallel feed {d.count} docs, {P} procs x {T} threads", flush=True)
    q, t0 = Queue(), time.time()
    ps = [Process(target=worker, args=(w, P, T, d.base_path, d.count, q, start)) for w in range(P)]
    for p in ps: p.start()
    done, errs, fin, nxt = start, 0, 0, start + 1_000_000
    while fin < P:
        m = q.get()
        if m is None: fin += 1; continue
        done += m[0]; errs += m[1]
        if done >= nxt:
            print(f"  fed {done:>9}/{d.count}  {time.time()-t0:7.1f}s  {done/(time.time()-t0):6.0f}/s errors={errs}", flush=True)
            nxt += 1_000_000
    for p in ps: p.join()
    print(f"  feed {time.time()-t0:.1f}s docs={done} errors={errs}", flush=True)
    if errs or done != d.count: sys.exit(1)  # done counts from start
    ok, info = E.wait_indexed(ds, d.count, log=lambda s: print(s, flush=True))
    print(f"  settled ok={ok} {info}", flush=True)
    sys.exit(0 if ok else 1)
