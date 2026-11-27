"""Shared pieces of the FP32 vs SQ8 memory-footprint experiment.

Dataset
-------
cohere1m (768-d, euclidean) is the base.  NOTE: despite cohere1m_meta.json saying
"normalized", the vectors have L2 norm ~13.8 (min 12.4, max 16.7; measured); the
median query->NN distance is ~7.0.  Vector id i < 1M is the real cohere vector i.
For i >= 1M the vector is cohere[i % 1M] + N(0, SIGMA^2) noise (noise norm ~1.4),
rescaled back to the source vector's norm, generated deterministically per 10k-id block -- the
"augmented" ids exist only so that N can grow past 1M; memory footprint depends
on N x D and the graph degree, not on the values.  Recall is computed against an
exact brute-force ground truth over exactly the ids [0, N) that were inserted.
"""
import json, os, subprocess, sys, threading, time
import numpy as np

ROOT = "/home/dev_path/experiments/memfootprint-sq8"
BASE = "/home/dev_path/datasets/cohere1m/cohere1m_base.fvecs"
QUERY = "/home/dev_path/datasets/cohere1m/cohere1m_query.fvecs"
DIM = 768
REAL_N = 1_000_000
BLOCK = 10_000
SIGMA = 0.05
NQ_RECALL = 200          # queries with exact GT, used for recall@10
TOPK = 10

# HNSW parameters, identical across engines (memory depends on M, not efC)
HNSW_M = 30              # same M as the earlier workload-generality runs
HNSW_EFC = 128           # lowered from 360 for build time; does not change memory
EF_SEARCH = 128          # Milvus ef / Qdrant hnsw_ef / Vespa targetHits+explore = 10+118

_base = None
def base():
    global _base
    if _base is None:
        raw = np.memmap(BASE, dtype=np.float32, mode="r")
        _base = raw.reshape(-1, DIM + 1)[:, 1:]
        assert _base.shape[0] == REAL_N
    return _base

def queries():
    raw = np.fromfile(QUERY, dtype=np.float32).reshape(-1, DIM + 1)[:, 1:]
    return np.ascontiguousarray(raw)

def block(b):
    """Vectors for ids [b*BLOCK, (b+1)*BLOCK) as float32 (BLOCK, DIM)."""
    lo = b * BLOCK
    src = lo % REAL_N
    v = np.array(base()[src:src + BLOCK], dtype=np.float32)
    if lo >= REAL_N:
        rng = np.random.default_rng(1_000_003 * b + 17)
        n0 = np.linalg.norm(v, axis=1, keepdims=True)
        v += rng.normal(0.0, SIGMA, size=v.shape).astype(np.float32)
        v *= n0 / np.linalg.norm(v, axis=1, keepdims=True)
    return v

def vectors(lo, hi):
    """ids [lo, hi), lo and hi multiples of BLOCK."""
    assert lo % BLOCK == 0 and hi % BLOCK == 0
    return np.concatenate([block(b) for b in range(lo // BLOCK, hi // BLOCK)])

# --- external int8 quantisation used for the Vespa "SQ8" arm -------------------
# Vespa has no engine-side float->int8 scalar quantisation (indexing language has
# only binarize/pack_bits), so the int8 tensor is produced here.  ONE global
# symmetric scale is used (not per-dimension min/max like Milvus SQ8): with a
# per-dimension scale, euclidean distance over the int8 codes would be a
# re-weighted distance, because Vespa computes distance on the stored int8 values
# directly with no de-quantisation.
# The clip value was chosen by brute-force int8 recall@10 on the first 60k ids
# (200 queries): clip 1.0 -> 0.9605, 1.5 -> 0.9760, 2.0 -> 0.9755, 3.0 -> 0.9750,
# 5.48 (=max|x|) -> 0.9710.  |x| 99th pct = 1.17, 99.9th = 5.04 is dominated by
# a few outlier dimensions.  2.0 is used (flat region, less clipping).
INT8_CLIP = 2.0
def int8_scale():
    p = os.path.join(ROOT, "data", "int8_scale.json")
    scale = INT8_CLIP / 127.0
    if not os.path.exists(p):
        json.dump({"clip": INT8_CLIP, "scale": scale,
                   "rule": "q = clip(round(x/scale), -127, 127); same rule for docs and queries"},
                  open(p, "w"), indent=1)
    return scale

def to_int8(v, scale):
    return np.clip(np.rint(v / scale), -127, 127).astype(np.int8)

# --- exact ground truth, grown incrementally (steps are prefixes) ------------
def ground_truth(n, log=print):
    """top-10 ids over ids [0, n) for the first NQ_RECALL queries; cached."""
    os.makedirs(os.path.join(ROOT, "data", "gt"), exist_ok=True)
    path = lambda k: os.path.join(ROOT, "data", "gt", f"gt_{k}.npz")
    if os.path.exists(path(n)):
        return np.load(path(n))["ids"]
    done = sorted(int(f[3:-4]) for f in os.listdir(os.path.join(ROOT, "data", "gt"))
                  if f.startswith("gt_") and int(f[3:-4]) < n)
    q = queries()[:NQ_RECALL].astype(np.float32)
    if done:
        start = done[-1]
        z = np.load(path(start)); best_d, best_i = z["d"], z["ids"]
    else:
        start = 0
        best_d = np.full((NQ_RECALL, TOPK), np.inf, np.float32)
        best_i = np.full((NQ_RECALL, TOPK), -1, np.int64)
    t0 = time.time()
    qq = (q * q).sum(1, keepdims=True)
    for lo in range(start, n, 100_000):
        hi = min(n, lo + 100_000)
        x = vectors(lo, hi)
        d = qq - 2.0 * q @ x.T + (x * x).sum(1)[None, :]
        ids = np.arange(lo, hi)[None, :].repeat(NQ_RECALL, 0)
        cd = np.concatenate([best_d, d], 1); ci = np.concatenate([best_i, ids], 1)
        sel = np.argpartition(cd, TOPK, axis=1)[:, :TOPK]
        best_d = np.take_along_axis(cd, sel, 1); best_i = np.take_along_axis(ci, sel, 1)
        o = np.argsort(best_d, 1)
        best_d = np.take_along_axis(best_d, o, 1); best_i = np.take_along_axis(best_i, o, 1)
    tmp = path(n)[:-4] + f".tmp{os.getpid()}.npz"
    np.savez(tmp, d=best_d, ids=best_i)
    os.replace(tmp, path(n))
    log(f"  GT for N={n} computed from {start} in {time.time()-t0:.0f}s")
    return best_i

def recall(found, gt):
    hit = sum(len(set(f[:TOPK]) & set(g[:TOPK].tolist())) for f, g in zip(found, gt))
    return hit / (len(gt) * TOPK)

# --- k8s helpers ---------------------------------------------------------------
KC = [os.path.join(ROOT, "scripts", "kc.sh")]

def kc(*args, inp=None, timeout=300, check=True):
    r = subprocess.run(KC + list(args), input=inp, capture_output=True, text=True,
                       timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} -> {r.returncode}: {r.stderr[-800:]}")
    return r.stdout

def pexec(pod, cmd, timeout=300, check=True):
    return kc("-n", "memexp", "exec", pod, "--", "bash", "-c", cmd,
              timeout=timeout, check=check)

def pod_status(pod):
    out = kc("-n", "memexp", "get", "pod", pod, "-o", "json", check=False)
    try:
        j = json.loads(out)
    except Exception:
        return {"phase": "Missing"}
    st = j.get("status", {})
    cs = (st.get("containerStatuses") or [{}])[0]
    term = (cs.get("state", {}).get("terminated") or cs.get("lastState", {}).get("terminated") or {})
    return {"phase": st.get("phase"), "ip": st.get("podIP"),
            "restarts": cs.get("restartCount", 0), "terminated_reason": term.get("reason"),
            "exit_code": term.get("exitCode")}

def create_pod(system, name, mem, log=print, restart="Never"):
    y = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gen_pod.py"),
                        system, name, mem, restart], capture_output=True, text=True, check=True).stdout
    open(os.path.join(ROOT, "k8s", f"{name}.yaml"), "w").write(y)
    kc("apply", "-f", "-", inp=y)
    for _ in range(120):
        s = pod_status(name)
        if s["phase"] == "Running" and s["ip"]:
            log(f"pod {name} running at {s['ip']} (limit {mem})")
            return s["ip"]
        time.sleep(5)
    raise RuntimeError(f"pod {name} never became Running: {s}")

def delete_pod(name):
    kc("-n", "memexp", "delete", "pod", name, "--ignore-not-found", "--wait=true", timeout=300,
       check=False)

# --- sampler stream ------------------------------------------------------------
def parse_sample(line):
    d = {}
    for tok in line.split():
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if k == "procs":
            procs = []
            for p in v.split("|"):
                if p.count(":") == 5:
                    n, pid, r, a, f, h = p.split(":")
                    procs.append((n, int(pid), int(r), int(a), int(f), int(h)))
            d["procs"] = procs
        else:
            try:
                d[k] = int(v)
            except ValueError:
                d[k] = v
    return d

class Sampler:
    """Streams scripts/sampler.sh from inside the pod into a raw log + memory.

    The raw line is kept verbatim in <run>/samples.log; phases are marked with
    `mark()` so every number in the CSV can be traced back to raw samples.
    """
    def __init__(self, pod, logpath, interval=0.5):
        self.pod, self.logpath, self.interval = pod, logpath, interval
        self.samples, self.lock = [], threading.Lock()
        self.stop_ev = threading.Event()
        self.proc = None
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def _run(self):
        script = open(os.path.join(ROOT, "scripts", "sampler.sh")).read()
        with open(self.logpath, "a") as fh:
            while not self.stop_ev.is_set():
                self.proc = subprocess.Popen(
                    KC + ["-n", "memexp", "exec", "-i", self.pod, "--", "bash", "-s",
                          str(self.interval)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True)
                self.proc.stdin.write(script); self.proc.stdin.close()
                for line in self.proc.stdout:
                    fh.write(line); fh.flush()
                    d = parse_sample(line)
                    if "cg_cur" in d:
                        with self.lock:
                            self.samples.append(d)
                    if self.stop_ev.is_set():
                        break
                self.proc.kill()
                if not self.stop_ev.is_set():
                    fh.write(f"# sampler stream ended at {time.time():.3f}, reconnecting\n")
                    time.sleep(3)

    def mark(self, text):
        with open(self.logpath, "a") as fh:
            fh.write(f"# MARK {int(time.time()*1000)} {text}\n")

    def window(self, t0, t1):
        a, b = int(t0 * 1000), int(t1 * 1000)
        with self.lock:
            return [s for s in self.samples if a <= s.get("t_ms", 0) <= b]

    def last(self):
        with self.lock:
            return self.samples[-1] if self.samples else None

    def stop(self):
        self.stop_ev.set()
        try:
            self.proc.kill()
        except Exception:
            pass
        # kill the in-pod loop as well (the exec'd bash outlives the stream)
        try:
            pexec(self.pod, 'for p in /proc/[0-9]*; do c=$(tr "\\0" " " < $p/cmdline 2>/dev/null); '
                            'case "$c" in "bash -s"*) kill ${p#/proc/};; esac; done', timeout=30,
                  check=False)
        except Exception:
            pass

def summarize(samples, main_procs):
    """Collapse a window of samples into the reported memory numbers (GiB)."""
    G = 1024 ** 3
    if not samples:
        return {}
    def proc_sum(s, idx):
        return sum(p[idx] for p in s.get("procs", []) if p[0] in main_procs) * 1024
    def all_sum(s, idx):
        return sum(p[idx] for p in s.get("procs", [])) * 1024
    last = samples[-1]
    out = {
        "cg_cur_gb": last["cg_cur"] / G,
        "cg_anon_gb": last.get("anon", 0) / G,
        "cg_file_gb": last.get("file", 0) / G,
        "rss_gb": proc_sum(last, 2) / G,           # engine process(es)
        "rss_anon_gb": proc_sum(last, 3) / G,
        "rss_file_gb": proc_sum(last, 4) / G,
        "rss_allprocs_gb": all_sum(last, 2) / G,   # every process in the pod
        "node_avail_gb": last.get("node_avail", 0) / 1024 ** 2,
        "node_used_gb": (last.get("node_total", 0) - last.get("node_avail", 0)) / 1024 ** 2,
        "peak_cg_cur_gb": max(s["cg_cur"] for s in samples) / G,
        "peak_cg_anon_gb": max(s.get("anon", 0) for s in samples) / G,
        "peak_rss_gb": max(proc_sum(s, 2) for s in samples) / G,
        "peak_rss_anon_gb": max(proc_sum(s, 3) for s in samples) / G,
        "cg_peak_lifetime_gb": last.get("cg_peak", -1) / G,
        "oom_kill": last.get("oom_kill", 0),
        "events_max": last.get("max", 0),
        "events_high": last.get("high", 0),
        "n_samples": len(samples),
        "majfault_delta": last.get("pgmajfault", 0) - samples[0].get("pgmajfault", 0),
        "refault_file_delta": last.get("workingset_refault_file", 0) - samples[0].get("workingset_refault_file", 0),
    }
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}
