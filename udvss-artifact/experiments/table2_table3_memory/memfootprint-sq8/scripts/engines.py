"""Engine adapters: Milvus / Qdrant / Vespa, each in FP32 or SQ8 mode.

Every adapter exposes
    wait_ready()            server up (Vespa: + application deployed)
    setup()                 create the collection / schema (empty)
    insert(lo, hi)          ids [lo, hi); raises EngineFailure on memory-pressure errors
    settle()                block until the data is indexed AND serving (Milvus: loaded)
    search(qvec) -> ids     one top-10 query
    reported()              what the DB itself reports about vector/index memory
    config()                the exact settings, saved next to every run
"""
import json, os, re, time, threading
import numpy as np
import requests
import common as C

class EngineFailure(Exception):
    """A memory-pressure failure reported by the engine itself (not a bug)."""
    def __init__(self, kind, msg):
        super().__init__(f"{kind}: {msg}")
        self.kind, self.msg = kind, msg

# =============================================================================
class Qdrant:
    system = "qdrant"
    main_procs = {"qdrant"}
    NAME = "mf"

    def __init__(self, ip, mode):
        from qdrant_client import QdrantClient
        self.ip, self.mode = ip, mode
        self.c = QdrantClient(host=ip, port=6333, grpc_port=6334, prefer_grpc=True, timeout=600)

    def config(self):
        return {
            "vectors": {"size": C.DIM, "distance": "Euclid",
                        # Qdrant always keeps vectors in a mmap'd file; 'cached'
                        # pre-loads it into page cache (default), 'cold' does not.
                        "memory": "cached" if self.mode == "fp32" else "cold"},
            "hnsw": {"m": C.HNSW_M, "ef_construct": C.HNSW_EFC, "on_disk": False},
            "quantization": None if self.mode == "fp32" else
                {"scalar": {"type": "int8", "quantile": 0.99, "memory": "pinned"}},
            "search": {"hnsw_ef": C.EF_SEARCH,
                       "quantization": None if self.mode == "fp32" else
                           {"rescore": False, "ignore": False}},
            "note": ("SQ8 = official low-RAM hybrid: original FP32 stays on disk ('cold', "
                     "mmap, page cache only if touched), int8 copy pinned in RAM, "
                     "no rescoring (so originals are not read at query time)."),
        }

    def wait_ready(self, timeout=600):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                requests.get(f"http://{self.ip}:6333/", timeout=5).raise_for_status()
                return
            except Exception:
                time.sleep(3)
        raise RuntimeError("qdrant not ready")

    def setup(self):
        from qdrant_client import models as m
        if self.c.collection_exists(self.NAME):
            self.c.delete_collection(self.NAME)
        q = None
        if self.mode == "sq8":
            q = m.ScalarQuantization(scalar=m.ScalarQuantizationConfig(
                type=m.ScalarType.INT8, quantile=0.99, memory=m.Memory.PINNED))
        self.c.create_collection(
            collection_name=self.NAME,
            vectors_config=m.VectorParams(
                size=C.DIM, distance=m.Distance.EUCLID,
                memory=m.Memory.CACHED if self.mode == "fp32" else m.Memory.COLD),
            hnsw_config=m.HnswConfigDiff(m=C.HNSW_M, ef_construct=C.HNSW_EFC, on_disk=False),
            quantization_config=q, shard_number=1, replication_factor=1)
        self.server_config = json.loads(
            requests.get(f"http://{self.ip}:6333/collections/{self.NAME}", timeout=30).text)

    def insert(self, lo, hi):
        from qdrant_client import models as m
        for b0 in range(lo, hi, 20_000):
            v = C.vectors(b0, min(hi, b0 + 20_000))
            try:
                for i in range(0, len(v), 500):
                    self.c.upsert(self.NAME, wait=False, points=m.Batch(
                        ids=list(range(b0 + i, b0 + i + len(v[i:i + 500]))),
                        vectors=v[i:i + 500].tolist()))
            except Exception as e:
                raise EngineFailure("insert_error", str(e)[:500])

    def settle(self, expect, timeout=4 * 3600, poll=10, stable=3, log=print):
        t0, streak = time.time(), 0
        while time.time() - t0 < timeout:
            try:
                ci = self.c.get_collection(self.NAME)
                n = self.c.count(self.NAME, exact=True).count
            except Exception as e:
                raise EngineFailure("unreachable", str(e)[:300])
            st = str(ci.status).split(".")[-1].lower()
            opt = str(ci.optimizer_status).split(".")[-1].lower()
            good = st == "green" and opt == "ok" and n == expect
            streak = streak + 1 if good else 0
            if streak >= stable:
                self.last_info = {"status": st, "segments": ci.segments_count,
                                  "indexed_vectors": ci.indexed_vectors_count, "points": n}
                return time.time() - t0
            if int(time.time() - t0) % 120 < poll:
                log(f"    qdrant settle: status={st} opt={opt} count={n}/{expect} "
                    f"indexed={ci.indexed_vectors_count} segs={ci.segments_count}")
            time.sleep(poll)
        raise EngineFailure("settle_timeout", f"not green after {timeout}s")

    def search(self, q):
        from qdrant_client import models as m
        p = m.SearchParams(hnsw_ef=C.EF_SEARCH, exact=False,
                           quantization=None if self.mode == "fp32" else
                           m.QuantizationSearchParams(rescore=False, ignore=False))
        r = self.c.query_points(self.NAME, query=q.tolist(), limit=C.TOPK, search_params=p,
                                with_payload=False, with_vectors=False)
        return [pt.id for pt in r.points]

    def reported(self, pod):
        """Sizes of the on-disk component files (= what Qdrant mmaps / pins) and
        Qdrant's own allocator metrics from /metrics."""
        out = C.pexec(pod, "find /qdrant/storage/collections/mf -type f -printf '%s %P\\n'",
                      check=False)
        comp = {"vectors_fp32": 0, "quantized": 0, "hnsw_graph": 0, "wal": 0, "other": 0}
        for line in out.splitlines():
            try:
                sz, path = line.split(" ", 1); sz = int(sz)
            except ValueError:
                continue
            p = path.lower()
            if "quantiz" in p:
                comp["quantized"] += sz
            elif "/vector_index/" in p or "graph" in p or "links" in p:
                comp["hnsw_graph"] += sz
            elif "/vector_storage" in p:
                comp["vectors_fp32"] += sz
            elif p.startswith("0/wal") or "/wal/" in p or p.startswith("wal"):
                comp["wal"] += sz
            else:
                comp["other"] += sz
        G = 1024 ** 3
        r = {f"q_{k}_gb": round(v / G, 4) for k, v in comp.items()}
        try:
            met = requests.get(f"http://{self.ip}:6333/metrics", timeout=30).text
            for k in ("memory_active_bytes", "memory_allocated_bytes", "memory_resident_bytes",
                      "memory_retained_bytes"):
                mm = re.search(rf"^{k}\s+(\S+)", met, re.M)
                if mm:
                    r[f"q_{k}_gb"] = round(float(mm.group(1)) / G, 4)
        except Exception:
            pass
        # "index/vector memory" as Qdrant would hold it RESIDENT in this config
        if self.mode == "fp32":
            r["db_vec_index_gb"] = round((comp["vectors_fp32"] + comp["hnsw_graph"]) / G, 4)
        else:
            r["db_vec_index_gb"] = round((comp["quantized"] + comp["hnsw_graph"]) / G, 4)
        r["db_report_src"] = "segment files (vector_storage/quantized/vector_index) + jemalloc /metrics"
        return r

# =============================================================================
class Milvus:
    system = "milvus"
    main_procs = {"milvus"}
    NAME = "mf"

    def __init__(self, ip, mode):
        from pymilvus import connections
        self.ip, self.mode = ip, mode
        self.alias = f"a{os.getpid()}_{threading.get_ident()}"
        connections.connect(alias=self.alias, host=ip, port="19530", timeout=60)

    def _coll(self):
        from pymilvus import Collection
        return Collection(self.NAME, using=self.alias)

    def index_params(self):
        if self.mode == "fp32":
            return {"index_type": "HNSW", "metric_type": "L2",
                    "params": {"M": C.HNSW_M, "efConstruction": C.HNSW_EFC}}
        return {"index_type": "HNSW_SQ", "metric_type": "L2",
                "params": {"M": C.HNSW_M, "efConstruction": C.HNSW_EFC,
                           "sq_type": "SQ8", "refine": False}}

    def config(self):
        return {"index": self.index_params(),
                "search": {"metric_type": "L2", "params": {"ef": C.EF_SEARCH}},
                "server": "milvus v2.6.23 standalone, embedded etcd, local storage, "
                          "default milvus.yaml (no overrides)",
                "note": "SQ8 = native HNSW_SQ(sq_type=SQ8, refine=false): no FP32 refine copy "
                        "inside the index. Whether raw FP32 field data is ALSO loaded is "
                        "measured, not assumed."}

    def wait_ready(self, timeout=900):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if requests.get(f"http://{self.ip}:9091/healthz", timeout=5).text.strip() == "OK":
                    return
            except Exception:
                pass
            time.sleep(3)
        raise RuntimeError("milvus not ready")

    def setup(self):
        from pymilvus import utility, FieldSchema, CollectionSchema, DataType, Collection
        if utility.has_collection(self.NAME, using=self.alias):
            utility.drop_collection(self.NAME, using=self.alias)
        s = CollectionSchema([FieldSchema("id", DataType.INT64, is_primary=True, auto_id=False),
                              FieldSchema("vec", DataType.FLOAT_VECTOR, dim=C.DIM)])
        c = Collection(self.NAME, s, using=self.alias, shards_num=1)
        c.create_index("vec", self.index_params())
        self.loaded = False

    def insert(self, lo, hi):
        c = self._coll()
        if getattr(self, "loaded", False):
            # released before each growth step so that "after load" is a clean,
            # fully-sealed, fully-indexed load (no growing segments / interim index)
            c.release(); self.loaded = False
        for b0 in range(lo, hi, 10_000):
            v = C.vectors(b0, min(hi, b0 + 10_000))
            for i in range(0, len(v), 5000):
                ch = v[i:i + 5000]
                try:
                    c.insert([list(range(b0 + i, b0 + i + len(ch))), ch])
                except Exception as e:
                    raise EngineFailure("insert_denied", str(e)[:500])
        try:
            c.flush(timeout=3600)
        except Exception as e:
            raise EngineFailure("flush_error", str(e)[:500])

    def settle(self, expect, timeout=4 * 3600, poll=10, stable=3, log=print):
        from pymilvus import utility
        t0, streak = time.time(), 0
        while time.time() - t0 < timeout:
            try:
                p = utility.index_building_progress(self.NAME, using=self.alias)
            except Exception as e:
                raise EngineFailure("unreachable", str(e)[:300])
            good = (p.get("total_rows") == expect and p.get("indexed_rows") == expect
                    and p.get("pending_index_rows", 0) == 0)
            streak = streak + 1 if good else 0
            if streak >= stable:
                break
            if int(time.time() - t0) % 120 < poll:
                log(f"    milvus index: {p}")
            time.sleep(poll)
        else:
            raise EngineFailure("index_timeout", f"index not built after {timeout}s")
        t_index = time.time() - t0
        if getattr(self, "restart_hook", None):
            # clean-reload variant: restart the container so the process that
            # loads the segments has never built an index
            self.restart_hook()
        self.load_t0 = time.time()
        try:
            self._coll().load(timeout=timeout)
            utility.wait_for_loading_complete(self.NAME, using=self.alias, timeout=timeout)
        except Exception as e:
            raise EngineFailure("load_rejected", str(e)[:800])
        self.loaded = True
        self.load_t1 = time.time()
        self.last_info = {"index_seconds": round(t_index, 1),
                          "load_seconds": round(time.time() - t0 - t_index, 1)}
        return time.time() - t0

    def search(self, q):
        r = self._coll().search([q.tolist()], "vec",
                                {"metric_type": "L2", "params": {"ef": C.EF_SEARCH}},
                                limit=C.TOPK)
        return list(r[0].ids)

    def reported(self, pod):
        """Milvus 2.6 returns mem_size=0 from get_query_segment_info, and
        querynode_entity_size does not count vectors (identical for FP32/SQ8), so the
        DB-side number is the querynode loader's OWN per-segment memory prediction
        (segment_loader.go "predict memory and disk usage while loading"), summed over
        the segments of the most recent load.  That prediction is what Milvus compares
        against memLimit (= cgroup limit) to admit or reject a load."""
        from pymilvus import utility
        r = {}
        try:
            segs = utility.get_query_segment_info(self.NAME, using=self.alias)
            r["m_segments"] = len(segs)
            r["m_seg_rows"] = sum(s.num_rows for s in segs)
            r["m_index_names"] = ";".join(sorted({s.index_name for s in segs}))
        except Exception as e:
            r["m_segments_err"] = str(e)[:200]
        logs = C.kc("-n", "memexp", "logs", pod, check=False, timeout=180)
        pred = [l for l in logs.splitlines() if "predict memory and disk usage while loading" in l]
        if pred:
            # every predict line logged between load() start and load completion
            # (one line per segment batch); falls back to the last traceID
            import datetime as _dt
            def _ts(l):
                m = re.match(r"\[(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\.\d+)", l)
                return _dt.datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S.%f").replace(
                    tzinfo=_dt.timezone.utc).timestamp() if m else None
            t0, t1 = getattr(self, "load_t0", None), getattr(self, "load_t1", None)
            last = [l for l in pred if t0 and _ts(l) and t0 - 2 <= _ts(l) <= t1 + 2]
            if not last:
                tid = re.search(r"traceID=(\w+)", pred[-1]).group(1)
                last = [l for l in pred if f"traceID={tid}" in l]
            seg = sum(float(re.search(r"maxSegmentSize\(MB\)=([\d.]+)", l).group(1)) for l in last)
            r["m_load_pred_seg_mem_gb"] = round(seg / 1024, 4)
            r["m_mem_limit_mb"] = float(re.search(r"memLimit\(MB\)=([\d.]+)", last[-1]).group(1))
            r["m_pred_lines"] = len(last)
            r["db_vec_index_gb"] = r["m_load_pred_seg_mem_gb"]
            r["db_report_src"] = "querynode loader predicted segment memory (sum over last load)"
        du = C.pexec(pod, "du -sb /var/lib/milvus/data/index_files /var/lib/milvus/data/insert_log 2>/dev/null",
                     check=False, timeout=120)
        for l in du.splitlines():
            sz, path = l.split("\t") if "\t" in l else l.split(None, 1)
            r["m_disk_" + os.path.basename(path.strip()) + "_gb"] = round(int(sz) / 1024 ** 3, 4)
        try:
            met = requests.get(f"http://{self.ip}:9091/metrics", timeout=30).text
            r["_metrics_lines"] = [l for l in met.splitlines() if l.startswith(
                ("milvus_querynode_entity", "milvus_querynode_disk_used", "process_resident"))]
        except Exception:
            pass
        return r

# =============================================================================
class Vespa:
    system = "vespa"
    main_procs = {"vespa-proton-bi"}       # /proc/<pid>/status Name is 15 chars
    SCHEMA = "vec"

    def __init__(self, ip, mode, pod=None):
        self.ip, self.mode, self.pod = ip, mode, pod
        self.cell = "float" if mode == "fp32" else "int8"
        self.scale = C.int8_scale() if mode == "sq8" else None
        self.s = requests.Session()
        self.s.mount("http://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=64))

    def config(self):
        return {"field": f"tensor<{self.cell}>(x[{C.DIM}])", "distance-metric": "euclidean",
                "hnsw": {"max-links-per-node": C.HNSW_M,
                         "neighbors-to-explore-at-insert": C.HNSW_EFC},
                "query": {"targetHits": C.TOPK,
                          "hnsw.exploreAdditionalHits": C.EF_SEARCH - C.TOPK,
                          "query_tensor": f"tensor<{self.query_cell()}>(x[{C.DIM}])"},
                "proton_hwinfo_cpu_cores": 8,
                "resource_limits": "Vespa defaults (no override)",
                "int8_quantisation": None if self.mode == "fp32" else
                    {"where": "client side, before feeding (NOT engine-native SQ8)",
                     "rule": "q = clip(round(x/scale), -127, 127), one global symmetric scale",
                     "scale": self.scale,
                     "original_fp32_kept_in_vespa": False}}

    def query_cell(self):
        return "float" if self.mode == "fp32" else "int8"

    def services(self):
        return f"""<?xml version="1.0" encoding="utf-8" ?>
<services version="1.0">
  <container id="default" version="1.0">
    <document-api/>
    <search/>
    <nodes><node hostalias="node1"/></nodes>
  </container>
  <content id="vecs" version="1.0">
    <redundancy>1</redundancy>
    <config name="vespa.config.search.core.proton">
      <hwinfo><cpu><cores>8</cores></cpu></hwinfo>
    </config>
    <documents><document type="{self.SCHEMA}" mode="index"/></documents>
    <nodes><node hostalias="node1" distribution-key="0"/></nodes>
  </content>
</services>
"""

    def schema(self):
        return f"""schema {self.SCHEMA} {{
  document {self.SCHEMA} {{
    field doc_id type int {{
      indexing: attribute | summary
    }}
    field vector type tensor<{self.cell}>(x[{C.DIM}]) {{
      indexing: attribute | index
      attribute {{ distance-metric: euclidean }}
      index {{
        hnsw {{
          max-links-per-node: {C.HNSW_M}
          neighbors-to-explore-at-insert: {C.HNSW_EFC}
        }}
      }}
    }}
  }}
  rank-profile ann {{
    inputs {{ query(q) tensor<{self.query_cell()}>(x[{C.DIM}]) }}
    first-phase {{ expression: closeness(field, vector) }}
  }}
}}
"""

    def wait_ready(self, timeout=900):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                j = requests.get(f"http://{self.ip}:19071/state/v1/health", timeout=5).json()
                if j["status"]["code"] == "up":
                    break
            except Exception:
                pass
            time.sleep(3)
        else:
            raise RuntimeError("vespa config server not ready")

    def setup(self):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("services.xml", self.services())
            z.writestr("hosts.xml", f'<?xml version="1.0" encoding="utf-8" ?>\n<hosts>\n'
                                    f'  <host name="{self.pod}"><alias>node1</alias></host>\n</hosts>\n')
            z.writestr(f"schemas/{self.SCHEMA}.sd", self.schema())
        self.package = buf.getvalue()
        r = requests.post(f"http://{self.ip}:19071/application/v2/tenant/default/prepareandactivate",
                          data=self.package, headers={"Content-Type": "application/zip"},
                          timeout=900)
        if r.status_code != 200:
            raise RuntimeError(f"deploy failed {r.status_code}: {r.text[:800]}")
        t0 = time.time()
        while time.time() - t0 < 900:
            try:
                if self._count() == 0:
                    return
            except Exception:
                pass
            time.sleep(5)
        raise RuntimeError("vespa container never served")

    def _doc(self, i, v):
        vals = v.tolist() if self.mode == "fp32" else C.to_int8(v, self.scale).tolist()
        return {"fields": {"doc_id": int(i), "vector": {"values": vals}}}

    def _put(self, i, v):
        # A dead (e.g. OOMKilled) pod must end the feed quickly: after 50 connection
        # failures in this process every remaining put returns immediately.
        # (Before this fix a 4Gi OOM left the feeder retrying for hours.)
        if getattr(self, "_conn_fail", 0) > 50:
            return ("conn", "aborted: server unreachable")
        url = f"http://{self.ip}:8080/document/v1/mf/{self.SCHEMA}/docid/{i}"
        body = json.dumps(self._doc(i, v))
        for attempt in range(8):
            try:
                r = self.s.post(url, data=body, headers={"Content-Type": "application/json"},
                                timeout=120)
            except Exception as e:
                if attempt < 2:
                    time.sleep(1 + attempt); continue
                self._conn_fail = getattr(self, "_conn_fail", 0) + 1
                return ("conn", str(e)[:200])
            if r.status_code == 200:
                return None
            if r.status_code == 507:          # resource exhausted == feed block
                return ("507", r.text[:400])
            if r.status_code in (429, 503) and attempt < 7:
                time.sleep(0.5 * (attempt + 1)); continue
            return (str(r.status_code), r.text[:300])

    def insert(self, lo, hi, procs=4, threads=32):
        """procs x threads concurrent /document/v1 puts (one Python process is
        GIL-bound well below Vespa's HNSW insert rate)."""
        import multiprocessing as mp
        span = ((hi - lo) // C.BLOCK + procs - 1) // procs * C.BLOCK
        jobs = [(self.ip, self.mode, self.pod, x, min(hi, x + span), threads)
                for x in range(lo, hi, span)]
        with mp.get_context("spawn").Pool(len(jobs)) as pool:
            res = pool.map(_vespa_feed_part, jobs)
        errs = {}
        for e in res:
            for k, (n, msg) in e.items():
                errs.setdefault(k, [0, msg])[0] += n
        if "507" in errs:
            raise EngineFailure("feed_blocked", f"{errs['507'][0]} x 507: {errs['507'][1]}")
        if errs:
            raise EngineFailure("feed_errors", json.dumps(errs)[:600])

    def _insert_part(self, lo, hi, threads):
        from concurrent.futures import ThreadPoolExecutor
        errs = {}
        with ThreadPoolExecutor(threads) as ex:
            for b0 in range(lo, hi, C.BLOCK):
                v = C.vectors(b0, min(hi, b0 + C.BLOCK))
                for e in ex.map(lambda j: self._put(b0 + j, v[j]), range(len(v))):
                    if e:
                        errs.setdefault(e[0], [0, e[1]])[0] += 1
                # stop early on feed block, a pile of errors, or a dead server
                if "507" in errs or sum(x[0] for x in errs.values()) > 100 \
                        or getattr(self, "_conn_fail", 0) > 50:
                    break
        return errs

    def _count(self):
        r = self.s.post(f"http://{self.ip}:8080/search/", json={
            "yql": f"select * from {self.SCHEMA} where true", "hits": 0,
            "ranking.profile": "unranked", "timeout": "60s"}, timeout=120)
        r.raise_for_status()
        return int(r.json()["root"]["fields"]["totalCount"])

    def settle(self, expect, timeout=3600, poll=10, stable=3, log=print):
        t0, streak, last = time.time(), 0, None
        while time.time() - t0 < timeout:
            try:
                n = self._count()
            except Exception as e:
                raise EngineFailure("unreachable", str(e)[:300])
            streak = streak + 1 if (n == expect and n == last) else 0
            last = n
            if streak >= stable:
                # flush memory-resident write buffers so "stable" is post-flush
                C.pexec(self.pod, "vespa-proton-cmd --local triggerFlush", check=False, timeout=600)
                return time.time() - t0
            time.sleep(poll)
        raise EngineFailure("settle_timeout", f"count never reached {expect}")

    def search(self, q):
        qv = q.tolist() if self.mode == "fp32" else C.to_int8(q, self.scale).tolist()
        body = {"yql": f"select doc_id from {self.SCHEMA} where "
                       f"{{targetHits:{C.TOPK},hnsw.exploreAdditionalHits:{C.EF_SEARCH - C.TOPK}}}"
                       f"nearestNeighbor(vector,q)",
                "input.query(q)": qv, "ranking.profile": "ann", "hits": C.TOPK,
                "timeout": "20s"}
        r = self.s.post(f"http://{self.ip}:8080/search/", json=body, timeout=60)
        r.raise_for_status()
        return [h["fields"]["doc_id"] for h in r.json()["root"].get("children", [])]

    def reported(self, pod):
        """proton's own accounting from its state API (port 19113 on this single-node app):
        the `vector` attribute's memoryUsage (tensor store + HNSW graph) and proton's
        resource-usage view used for feed blocking (relative to the cgroup limit)."""
        r = {}
        G = 1024 ** 3
        out = C.pexec(pod, "curl -s localhost:19113/state/v1/custom/component/documentdb/vec/subdb/ready/attribute/vector;"
                           " echo; curl -s localhost:19113/state/v1/custom/component/resourceusage", check=False,
                      timeout=120)
        parts = [x for x in out.splitlines() if x.strip().startswith("{")]
        try:
            att = json.loads(parts[0]); ru = json.loads(parts[1])
            mu = att["status"]["memoryUsage"]
            r["v_attr_allocated_gb"] = round(mu["allocatedBytes"] / G, 4)
            r["v_attr_used_gb"] = round(mu["usedBytes"] / G, 4)
            r["db_vec_index_gb"] = r["v_attr_allocated_gb"]
            r["db_report_src"] = "proton state API attribute vector memoryUsage.allocatedBytes"
            m = ru["memory"]
            r["v_mem_usage"] = m.get("usage"); r["v_mem_limit"] = m.get("limit")
            r["v_mem_reserved"] = m.get("reserved"); r["v_physical_gb"] = round(m.get("physicalMemory", 0) / G, 3)
            r["v_anon_rss_gb"] = round(m["stats"]["anonymousRss"] / G, 4)
            r["v_mapped_rss_gb"] = round(m["stats"]["mappedRss"] / G, 4)
            r["_vespa_attr"] = att; r["_vespa_resourceusage"] = ru
        except Exception as e:
            r["db_report_src"] = f"state API parse failed: {e}: {out[:300]}"
        try:
            met = C.pexec(pod, "curl -s localhost:19092/prometheus/v1/values | grep -E '^(memory_rss|content_proton_documentdb_memory_usage_allocated)'",
                          check=False, timeout=120)
            r["_vespa_service_rss"] = met.splitlines()
        except Exception:
            pass
        return r

def _vespa_feed_part(args):
    ip, mode, pod, lo, hi, threads = args
    return Vespa(ip, mode, pod)._insert_part(lo, hi, threads)

ENGINES = {"qdrant": Qdrant, "milvus": Milvus, "vespa": Vespa}

def make(system, ip, mode, pod=None):
    if system == "vespa":
        return Vespa(ip, mode, pod)
    return ENGINES[system](ip, mode)
