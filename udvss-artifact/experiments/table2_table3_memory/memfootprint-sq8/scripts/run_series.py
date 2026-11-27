#!/usr/bin/env python3
"""One series = one fresh engine pod (one system x one representation x one
memory limit), grown through the N steps until the last step or the first
memory-pressure failure.

Per step the row records three distinct phases, each from the in-pod sampler:
    build  : insert + index build/load window   -> peak during build
    stable : 30 s after settle, 20 s window     -> "after load" memory (last sample)
    query  : C-concurrent query load, 30 s       -> peak during query, QPS, latency
plus an idle row (empty collection, engine up) before the first step.

usage: run_series.py --system qdrant --mode sq8 --mem 32Gi --tag p1 --steps 100000,250000,...
"""
import argparse, fcntl, json, os, sys, time, traceback
import multiprocessing as mp
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import engines as E

QUERY_LOCK = os.path.join(C.ROOT, "logs", ".query.lock")
CSV_COLS = ["system", "mode", "mem_limit", "tag", "phase", "N", "dim", "raw_vec_gb_fp32",
            "raw_vec_gb_repr", "db_vec_index_gb", "db_report_src",
            "rss_gb", "rss_anon_gb", "rss_file_gb", "rss_allprocs_gb",
            "cg_cur_gb", "cg_anon_gb", "cg_file_gb",
            "peak_rss_gb", "peak_rss_anon_gb", "peak_cg_cur_gb", "peak_cg_anon_gb",
            "cg_peak_lifetime_gb", "node_used_gb", "node_avail_gb",
            "qps", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "recall10", "query_errors",
            "insert_s", "settle_s", "oom_kill", "events_max", "majfault_delta", "refault_file_delta",
            "failure", "failure_msg",
            "utc"]

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# ---- query load: separate processes so the Python client is not the limit ---
def _qworker(args):
    system, ip, mode, pod, qidx0, duration, t_start = args
    eng = E.make(system, ip, mode, pod)
    Q = C.queries()
    while time.time() < t_start:
        time.sleep(0.01)
    lat, errs, i = [], 0, qidx0
    t_end = t_start + duration
    while time.time() < t_end:
        q = Q[i % len(Q)]; i += 7
        t0 = time.perf_counter()
        try:
            eng.search(q)
            lat.append(time.perf_counter() - t0)
        except Exception:
            errs += 1
    return lat, errs

def query_load(system, ip, mode, pod, conc=16, duration=30):
    t_start = time.time() + 8          # let every worker connect first
    with mp.get_context("spawn").Pool(conc) as pool:
        res = pool.map(_qworker, [(system, ip, mode, pod, k * 61, duration, t_start)
                                  for k in range(conc)])
    lat = np.array([x for r in res for x in r[0]]) * 1000
    errs = sum(r[1] for r in res)
    t0, t1 = t_start, t_start + duration
    if len(lat) == 0:
        return dict(qps=0, lat_p50_ms=None, lat_p95_ms=None, lat_p99_ms=None,
                    query_errors=errs), t0, t1
    return dict(qps=round(len(lat) / duration, 1),
                lat_p50_ms=round(float(np.percentile(lat, 50)), 2),
                lat_p95_ms=round(float(np.percentile(lat, 95)), 2),
                lat_p99_ms=round(float(np.percentile(lat, 99)), 2),
                query_errors=errs), t0, t1

def measure_recall(eng, n):
    gt = C.ground_truth(n)
    Q = C.queries()[:C.NQ_RECALL]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        found = list(ex.map(eng.search, Q))
    return round(C.recall(found, gt), 4)

# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", required=True)
    ap.add_argument("--mode", required=True, choices=["fp32", "sq8"])
    ap.add_argument("--mem", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--steps", required=True)
    ap.add_argument("--conc", type=int, default=16)
    ap.add_argument("--qdur", type=int, default=30)
    ap.add_argument("--keep-pod", action="store_true")
    ap.add_argument("--restart-before-load", action="store_true",
                    help="Milvus only: after index build, release + restart the container, then load")
    ap.add_argument("--settle-timeout", type=int, default=None,
                    help="per-step build/settle timeout (s); default 4h at >=32Gi, 1h below "
                         "(a step that thrashes for an hour under the limit is recorded as a failure)")
    a = ap.parse_args()
    steps = [int(x) for x in a.steps.split(",")]
    pod = f"mf-{a.system}-{a.mode}-{a.tag}"
    rdir = os.path.join(C.ROOT, "results", a.tag, f"{a.system}-{a.mode}")
    os.makedirs(rdir, exist_ok=True)
    logf = open(os.path.join(rdir, "run.log"), "a")
    def log(*x):
        s = f"[{now()}] " + " ".join(str(y) for y in x)
        print(s, flush=True); logf.write(s + "\n"); logf.flush()
    json.dump(vars(a) | {"argv": sys.argv, "pod": pod, "started": now()},
              open(os.path.join(rdir, "invocation.json"), "w"), indent=1)
    csv_path = os.path.join(rdir, "rows.csv")
    if not os.path.exists(csv_path):
        open(csv_path, "w").write(",".join(CSV_COLS) + "\n")

    def emit(row):
        row = {k: row.get(k, "") for k in CSV_COLS}
        with open(csv_path, "a") as fh:
            fh.write(",".join('"%s"' % str(v).replace('"', "'") if isinstance(v, str) and "," in str(v)
                              else str(v) for v in row.values()) + "\n")
        with open(os.path.join(rdir, "rows.jsonl"), "a") as fh:
            fh.write(json.dumps(row) + "\n")
        log("ROW", json.dumps({k: row[k] for k in ("phase", "N", "rss_gb", "rss_anon_gb",
            "cg_cur_gb", "peak_cg_cur_gb", "db_vec_index_gb", "qps", "recall10", "failure")}))

    C.delete_pod(pod)
    ip = C.create_pod(a.system, pod, a.mem, log=log,
                      restart="Always" if a.restart_before_load else "Never")
    samp = C.Sampler(pod, os.path.join(rdir, "samples.log"))
    eng = E.make(a.system, ip, a.mode, pod)
    if a.restart_before_load:
        def _restart():
            from pymilvus import connections
            eng._coll().release(); eng.loaded = False
            r0 = C.pod_status(pod).get("restarts", 0)
            log("  restart-before-load: killing milvus (pid 1)")
            C.pexec(pod, "kill 1", check=False, timeout=60)
            for _ in range(120):
                time.sleep(5)
                s = C.pod_status(pod)
                if s.get("restarts", 0) > r0 and s.get("phase") == "Running":
                    break
            eng.wait_ready()
            connections.disconnect(eng.alias)
            connections.connect(alias=eng.alias, host=ip, port="19530", timeout=120)
            log(f"  restarted (restarts={C.pod_status(pod).get('restarts')})")
        eng.restart_hook = _restart
    base = dict(system=a.system, mode=a.mode, mem_limit=a.mem, tag=a.tag, dim=C.DIM)
    failure = None
    try:
        eng.wait_ready()
        eng.setup()
        json.dump(eng.config(), open(os.path.join(rdir, "engine_config.json"), "w"), indent=1)
        if a.system == "vespa":
            open(os.path.join(rdir, "vespa_app.zip"), "wb").write(eng.package)
        if hasattr(eng, "server_config"):
            json.dump(eng.server_config, open(os.path.join(rdir, "server_collection.json"), "w"),
                      indent=1)
        time.sleep(30)
        t0 = time.time(); samp.mark("idle"); time.sleep(20)
        emit(base | dict(phase="idle", N=0, raw_vec_gb_fp32=0, raw_vec_gb_repr=0, utc=now())
             | C.summarize(samp.window(t0, time.time()), eng.main_procs))
        prev = 0
        for n in steps:
            G = 1024 ** 3
            raw32 = n * C.DIM * 4 / G
            rawr = raw32 if a.mode == "fp32" else n * C.DIM / G
            row = base | dict(N=n, raw_vec_gb_fp32=round(raw32, 4), raw_vec_gb_repr=round(rawr, 4))
            # disk guards (client host + node)
            free_local = os.statvfs("/home/dev_path"); fl = free_local.f_bavail * free_local.f_frsize / G
            if fl < 15:
                failure = ("guard", f"/home/dev_path free {fl:.1f}G < 15G"); break
            # ---- build -----------------------------------------------------
            log(f"step N={n}: insert {prev}->{n}")
            samp.mark(f"build N={n}")
            tb = time.time()
            try:
                eng.insert(prev, n)
                ti = time.time() - tb
                st = eng.settle(n, log=log, timeout=a.settle_timeout or
                                (4 * 3600 if a.mem.endswith("Gi") and int(a.mem[:-2]) >= 32 else 3600))
            except E.EngineFailure as ef:
                s = C.pod_status(pod)
                kind = "OOMKilled" if s.get("terminated_reason") == "OOMKilled" else ef.kind
                failure = (kind, ef.msg)
                emit(row | dict(phase="build", failure=kind, failure_msg=ef.msg[:300], utc=now())
                     | C.summarize(samp.window(tb, time.time()), eng.main_procs))
                break
            emit(row | dict(phase="build", insert_s=round(ti, 1), settle_s=round(st, 1), utc=now())
                 | C.summarize(samp.window(tb, time.time()), eng.main_procs))
            info = getattr(eng, "last_info", {})
            if info:
                log("  settle info", json.dumps(info))
            prev = n
            # ---- stable ----------------------------------------------------
            time.sleep(30)
            ts = time.time(); samp.mark(f"stable N={n}"); time.sleep(20)
            rep = eng.reported(pod)
            extra = {k: v for k, v in rep.items() if k.startswith("_")}
            if extra:
                with open(os.path.join(rdir, "db_reported_raw.jsonl"), "a") as fh:
                    fh.write(json.dumps({"N": n, "utc": now()} | extra) + "\n")
            rep = {k: v for k, v in rep.items() if not k.startswith("_")}
            with open(os.path.join(rdir, "db_reported.jsonl"), "a") as fh:
                fh.write(json.dumps({"N": n, "utc": now()} | rep) + "\n")
            stable = C.summarize(samp.window(ts, time.time()), eng.main_procs)
            emit(row | dict(phase="stable", db_vec_index_gb=rep.get("db_vec_index_gb", ""),
                            db_report_src=rep.get("db_report_src", ""), utc=now()) | stable)
            # ---- query -----------------------------------------------------
            try:
                rc = measure_recall(eng, n)
            except Exception as e:
                rc = None; log("  recall failed", e)
            with open(QUERY_LOCK, "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                samp.mark(f"query N={n}")
                try:
                    qres, q0, q1 = query_load(a.system, ip, a.mode, pod, a.conc, a.qdur)
                finally:
                    fcntl.flock(lk, fcntl.LOCK_UN)
            qs = C.summarize(samp.window(q0, q1 + 1), eng.main_procs)
            s = C.pod_status(pod)
            fail = None
            if s.get("terminated_reason") == "OOMKilled" or s.get("phase") not in ("Running",):
                fail = s.get("terminated_reason") or s.get("phase")
            emit(row | dict(phase="query", db_vec_index_gb=rep.get("db_vec_index_gb", ""),
                            recall10=rc, failure=fail or "", utc=now()) | qres | qs)
            if fail:
                failure = (fail, json.dumps(s)); break
            if qres["qps"] == 0:
                failure = ("query_failed", f"{qres['query_errors']} errors, 0 successes"); break
    except Exception as e:
        s = C.pod_status(pod)
        kind = "OOMKilled" if s.get("terminated_reason") == "OOMKilled" else "exception"
        failure = (kind, f"{type(e).__name__}: {str(e)[:400]}")
        log("EXCEPTION", traceback.format_exc())
    finally:
        s = C.pod_status(pod)
        log("final pod status", json.dumps(s), "failure", failure)
        json.dump({"failure": failure, "pod_status": s, "ended": now(), "last_N_ok": prev},
                  open(os.path.join(rdir, "outcome.json"), "w"), indent=1)
        try:
            ev = C.kc("-n", "memexp", "get", "events", "--field-selector",
                      f"involvedObject.name={pod}", check=False)
            open(os.path.join(rdir, "k8s_events.txt"), "w").write(ev)
            open(os.path.join(rdir, "engine_pod.log"), "w").write(
                C.kc("-n", "memexp", "logs", pod, "--tail=-1", check=False, timeout=120))
            if a.system == "vespa":
                open(os.path.join(rdir, "vespa_log_tail.txt"), "w").write(C.pexec(
                    pod, "vespa-logfmt -l error,warning -N /opt/vespa/logs/vespa/vespa.log | tail -300",
                    check=False, timeout=120))
            if a.system == "milvus":
                open(os.path.join(rdir, "milvus_effective_config.yaml"), "w").write(C.pexec(
                    pod, "cat /milvus/configs/milvus.yaml", check=False, timeout=60))
        except Exception as e:
            log("log collection failed", e)
        samp.stop()
        if not a.keep_pod:
            C.delete_pod(pod)
    log("DONE")

if __name__ == "__main__":
    main()
