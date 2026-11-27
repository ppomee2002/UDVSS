#!/usr/bin/env python3
"""Qdrant counterpart of the Milvus S / QN figure.

  (a)(b) S sweep : peers = 1, shards = 1 / 4 / 16          (Milvus QN=1, S=1/4/16)
  (c)(d) QN sweep: shards = 16, peers = 1 / 2 / 4 / 8 / 16 (Milvus S=1 x 16 segments)
replication_factor = 1 everywhere. Each peer = one pod on 109 with the Milvus
4-CPU budget (common.CPU). Every config is a fresh cluster + fresh load,
shards placed evenly (shards/peers each). Queries go to peer 0, which fans out
to the shards and merges (Qdrant has no separate proxy).
Usage: qdrant_sweep.py [S|Q|all]
"""
import json
import sys
import time

import requests

import common as C

CSV = C.csv_path("qdrant_runs.csv")
NAME = "deep"
APP = "qd"


def statefulset(n):
    # unlimited mode: no limits and Qdrant's own thread defaults
    threads = (f'        - {{name: QDRANT__STORAGE__PERFORMANCE__MAX_SEARCH_THREADS, value: "{C.CPU}"}}\n'
               if C.CPU else "")
    resources = (f"""        resources:
          requests: {{cpu: "{C.CPU}", memory: "{C.MEM_REQ}"}}
          limits:   {{cpu: "{C.CPU}", memory: "{C.MEM}"}}
""" if C.CPU else "")
    return f"""
apiVersion: v1
kind: Service
metadata: {{name: {APP}, namespace: {C.NS}, labels: {{exp: sqn, app: {APP}}}}}
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector: {{app: {APP}}}
  ports: [{{name: http, port: 6333}}, {{name: grpc, port: 6334}}, {{name: p2p, port: 6335}}]
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {{name: {APP}, namespace: {C.NS}, labels: {{exp: sqn, app: {APP}}}}}
spec:
  serviceName: {APP}
  replicas: {n}
  podManagementPolicy: OrderedReady
  selector: {{matchLabels: {{app: {APP}}}}}
  template:
    metadata: {{labels: {{exp: sqn, app: {APP}}}}}
    spec:
      nodeSelector: {{kubernetes.io/hostname: {C.NODE}}}
      terminationGracePeriodSeconds: 5
      containers:
      - name: qdrant
        image: qdrant/qdrant:v1.19.1
        command: ["/bin/bash", "-c"]
        args:
        - |
          ID=${{HOSTNAME##*-}}
          URI=http://$HOSTNAME.{APP}.{C.NS}.svc.cluster.local:6335
          if [ "$ID" = 0 ]; then exec ./qdrant --uri $URI
          else exec ./qdrant --bootstrap http://{APP}-0.{APP}.{C.NS}.svc.cluster.local:6335 --uri $URI; fi
        env:
        - {{name: QDRANT__CLUSTER__ENABLED, value: "true"}}
        - {{name: QDRANT__TELEMETRY_DISABLED, value: "true"}}
{threads}        ports: [{{containerPort: 6333}}, {{containerPort: 6334}}, {{containerPort: 6335}}]
        readinessProbe: {{httpGet: {{path: /readyz, port: 6333}}, periodSeconds: 3}}
{resources}        volumeMounts: [{{name: data, mountPath: /qdrant/storage}}]
      volumes: [{{name: data, emptyDir: {{}}}}]
"""


def deploy(n):
    C.delete_selector(f"app={APP}")
    C.kc("apply", "-f", "-", stdin=statefulset(n))
    ps = C.wait_pods(f"app={APP}", n)
    ip0 = ps[0][1]
    t0 = time.time()
    while time.time() - t0 < 600:
        try:
            j = requests.get(f"http://{ip0}:6333/cluster", timeout=5).json()["result"]
            if len(j["peers"]) == n:
                return ps
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError(f"qdrant cluster did not form {n} peers")


def create_and_balance(ip0, shards, peers, segs_per_shard):
    from qdrant_client import QdrantClient, models as m
    c = QdrantClient(host=ip0, port=6333, grpc_port=6334, prefer_grpc=True, timeout=600)
    c.create_collection(NAME, vectors_config=m.VectorParams(size=96, distance=m.Distance.EUCLID),
                        shard_number=shards, replication_factor=1,
                        hnsw_config=m.HnswConfigDiff(m=C.HNSW_M, ef_construct=C.HNSW_EFC),
                        # every segment gets an HNSW graph, as every Milvus sealed segment
                        # does; the default threshold leaves small segments as plain scans
                        # Qdrant derives the per-shard segment count from the visible CPU
                        # count (126 segments per peer with no limits on 109), so pin it:
                        # (c) 16 shards x 1 = Milvus's 16 segments; (a) 4 per shard =
                        # Milvus's CHUNKS=4 S sweep
                        optimizers_config=m.OptimizersConfigDiff(
                            indexing_threshold=1, default_segment_number=segs_per_shard))
    # even placement: move shards until every peer holds shards/peers of them
    url = f"http://{ip0}:6333/collections/{NAME}/cluster"
    want = max(1, shards // peers)
    for _ in range(4 * shards):
        j = requests.get(url, timeout=30).json()["result"]
        place = {j["peer_id"]: [s["shard_id"] for s in j["local_shards"]]}
        for s in j["remote_shards"]:
            place.setdefault(s["peer_id"], []).append(s["shard_id"])
        allpeers = [p for p in requests.get(f"http://{ip0}:6333/cluster", timeout=30)
                    .json()["result"]["peers"]]
        for p in allpeers:
            place.setdefault(int(p), [])
        over = [p for p, s in place.items() if len(s) > want]
        under = [p for p, s in place.items() if len(s) < want]
        if not over or not under:
            return {str(p): sorted(s) for p, s in place.items()}
        r = requests.post(url, json={"move_shard": {"shard_id": place[over[0]][0],
                                                    "from_peer_id": over[0], "to_peer_id": under[0]}},
                          timeout=60)
        r.raise_for_status()
        time.sleep(3)
    raise RuntimeError("could not balance shards")


def load(ip0, base):
    from qdrant_client import QdrantClient, models as m
    c = QdrantClient(host=ip0, port=6333, grpc_port=6334, prefer_grpc=True, timeout=600)
    c.upload_collection(NAME, vectors=base, ids=range(len(base)), batch_size=2000, parallel=4,
                        wait=True)
    t0 = time.time()
    while time.time() - t0 < 4 * 3600:   # until every segment is HNSW-indexed and idle
        info = requests.get(f"http://{ip0}:6333/collections/{NAME}", timeout=60).json()["result"]
        if (info["status"] == "green" and info.get("points_count") == len(base)
                and info.get("indexed_vectors_count", 0) >= len(base)):
            return info
        time.sleep(10)
    raise RuntimeError(f"qdrant never settled: {info}")


def searcher(ip0, with_vec):
    from qdrant_client import QdrantClient, models as m
    c = QdrantClient(host=ip0, port=6333, grpc_port=6334, prefer_grpc=True, timeout=60)
    p = m.SearchParams(hnsw_ef=C.EF, exact=False)

    def s(v):
        r = c.query_points(NAME, query=v.tolist(), limit=C.TOPK, search_params=p,
                           with_payload=False, with_vectors=with_vec).points
        return [x.id for x in r]
    return s


def point(fam, shards, peers, base, qv, gt):
    labels = [f"{fam}_{t}_S{shards}_P{peers}_r{r}" for r in range(1, C.REPS + 1) for t in ("ID", "VEC")]
    if set(labels) <= C.done_labels(CSV):
        return
    C.log(f"== qdrant {fam} shards={shards} peers={peers}")
    try:
        ps = deploy(peers)
    except RuntimeError as e:          # raft occasionally misses the first window
        C.log(f"   deploy retry after: {e}")
        ps = deploy(peers)
    ip0 = ps[0][1]
    # segments per shard, fixed per family and never a function of N:
    # S = Milvus CHUNKS=4; Q = one Milvus segment per shard; E (S x QN
    # diagonal) = 1, the same as Q, so E's 16x16 is (c)'s shards=16/peers=16
    # row and only 1x1 and 4x4 are measured here
    segs = {"S": 4}.get(fam, 1)
    place = create_and_balance(ip0, shards, peers, segs)
    C.log(f"   placement {place}")
    C.log("   [verify] pods/node/resources: " + C.kc(
        "get", "pods", "-n", C.NS, "-l", f"app={APP}", "-o",
        "custom-columns=POD:.metadata.name,NODE:.spec.nodeName,RES:.spec.containers[0].resources",
        "--no-headers").replace("\n", " | "))
    t0 = time.time()
    load(ip0, base)
    info = requests.get(f"http://{ip0}:6333/collections/{NAME}", timeout=60).json()["result"]
    cfg = info["config"]
    C.log(f"   loaded + indexed in {time.time()-t0:.0f}s, segments(peer0 view)={info.get('segments_count')}"
          f" hnsw={cfg['hnsw_config']} default_segment_number="
          f"{cfg['optimizer_config'].get('default_segment_number')}")
    path = [p[0] for p in ps]
    done = C.done_labels(CSV)
    for r in range(1, C.REPS + 1):
        for t in ("ID", "VEC"):
            lab = f"{fam}_{t}_S{shards}_P{peers}_r{r}"
            if lab in done:
                continue
            C.run_row(lab, searcher(ip0, t == "VEC"), qv, gt, path,
                      dict(system="qdrant", family=fam, mode=t, shards=shards, nodes=peers, rep=r,
                           segs_per_shard=segs,
                           placement=json.dumps(place)), CSV)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    base, qv, gt = C.dataset()
    C.kc("create", "namespace", C.NS, check=False)
    if which == "one":                      # qdrant_sweep.py one SHARDS PEERS
        point("X", int(sys.argv[2]), int(sys.argv[3]), base, qv, gt)
        return
    if which in ("S", "all"):
        for s in (1, 4, 16):
            point("S", s, 1, base, qv, gt)
    if which == "E":                        # S x QN diagonal: shards = peers = N
        for n in (1, 4):                    # 16x16 = (c) shards=16, peers=16
            point("E", n, n, base, qv, gt)
    if which in ("Q", "all"):
        for n in (1, 2, 4, 8, 16):
            point("Q", 16, n, base, qv, gt)
    C.delete_selector(f"app={APP}")
    C.log("=== qdrant sweep done ===")


if __name__ == "__main__":
    main()
