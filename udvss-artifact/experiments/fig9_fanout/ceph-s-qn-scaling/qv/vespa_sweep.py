#!/usr/bin/env python3
"""Vespa counterpart of the Milvus QN sweep, (c)(d) only.

Flat distribution (one group), redundancy 1, content nodes = 1/2/4/8/16 on 109
with a 4-CPU budget per node (common.CPU). One container node (query
dispatch + merge, the Proxy's role) and the config server run on 94, where
the Milvus Proxy runs. Every query is dispatched to all content nodes.
Vespa has no S knob (buckets split/join automatically; groups are replicas,
not shards), so it is not part of (a)(b). Adding content nodes also
re-partitions the corpus: that coupling is Vespa's architecture.
Usage: vespa_sweep.py
"""
import concurrent.futures as cf
import io
import time
import zipfile

import requests

import common as C

CSV = C.csv_path("vespa_runs.csv")
DOM = f"{C.NS}.svc.cluster.local"
FRONT = "milvus-proxy94"


def sts(app, n, node, args, cpu=None, mem=None, ports=()):
    res = (f"""
        resources:
          requests: {{cpu: "{cpu}", memory: "{C.MEM_REQ}"}}
          limits:   {{cpu: "{cpu}", memory: "{mem}"}}""" if cpu else "")
    return f"""
apiVersion: v1
kind: Service
metadata: {{name: {app}, namespace: {C.NS}, labels: {{exp: sqn, sys: vespa, app: {app}}}}}
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector: {{app: {app}}}
  ports: [{", ".join(f"{{name: p{p}, port: {p}}}" for p in ports or (19071,))}]
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {{name: {app}, namespace: {C.NS}, labels: {{exp: sqn, sys: vespa, app: {app}}}}}
spec:
  serviceName: {app}
  replicas: {n}
  podManagementPolicy: Parallel
  selector: {{matchLabels: {{app: {app}}}}}
  template:
    metadata: {{labels: {{exp: sqn, sys: vespa, app: {app}}}}}
    spec:
      nodeSelector: {{kubernetes.io/hostname: {node}}}
      terminationGracePeriodSeconds: 5
      containers:
      - name: vespa
        image: vespaengine/vespa:8.754.14
        args: ["{args}"]
        env:
        - {{name: VESPA_CONFIGSERVERS, value: "vcfg-0.vcfg.{DOM}"}}
        - {{name: POD_NAME, valueFrom: {{fieldRef: {{fieldPath: metadata.name}}}}}}
        - {{name: VESPA_HOSTNAME, value: "$(POD_NAME).{app}.{DOM}"}}{res}
"""


def services(n):
    # unlimited mode: let proton size its thread pools from the host
    hw = (f"""    <config name="vespa.config.search.core.proton">
      <hwinfo><cpu><cores>{C.CPU}</cores></cpu></hwinfo>
    </config>
""" if C.CPU else "")
    nodes = "\n".join(f'      <node hostalias="n{i}" distribution-key="{i}"/>' for i in range(n))
    return f"""<?xml version="1.0" encoding="utf-8" ?>
<services version="1.0">
  <admin version="2.0">
    <configservers><configserver hostalias="cfg"/></configservers>
    <cluster-controllers><cluster-controller hostalias="cfg"/></cluster-controllers>
    <slobroks><slobrok hostalias="cfg"/></slobroks>
    <adminserver hostalias="cfg"/>
  </admin>
  <container id="default" version="1.0">
    <document-api/>
    <search/>
    <nodes><node hostalias="c0"/></nodes>
  </container>
  <content id="vecs" version="1.0">
    <redundancy>1</redundancy>
{hw}    <documents><document type="vec" mode="index"/></documents>
    <nodes>
{nodes}
    </nodes>
  </content>
</services>
"""


def hosts(n):
    h = [f'  <host name="vcfg-0.vcfg.{DOM}"><alias>cfg</alias></host>',
         f'  <host name="vct-0.vct.{DOM}"><alias>c0</alias></host>']
    h += [f'  <host name="vcn-{i}.vcn.{DOM}"><alias>n{i}</alias></host>' for i in range(n)]
    return '<?xml version="1.0" encoding="utf-8" ?>\n<hosts>\n' + "\n".join(h) + "\n</hosts>\n"


SCHEMA = f"""schema vec {{
  document vec {{
    field doc_id type int {{
      indexing: attribute | summary
    }}
    field vector type tensor<float>(x[96]) {{
      indexing: attribute | index | summary
      attribute {{ distance-metric: euclidean }}
      index {{
        hnsw {{
          max-links-per-node: {C.HNSW_M}
          neighbors-to-explore-at-insert: {C.HNSW_EFC}
        }}
      }}
    }}
  }}
  document-summary ids {{
    summary doc_id {{}}
  }}
  document-summary withvec {{
    summary doc_id {{}}
    summary vector {{}}
  }}
  rank-profile ann {{
    inputs {{ query(q) tensor<float>(x[96]) }}
    first-phase {{ expression: closeness(field, vector) }}
  }}
}}
"""


def deploy(n):
    C.delete_selector("sys=vespa")
    C.kc("apply", "-f", "-", stdin=sts("vcfg", 1, FRONT, "configserver,services", ports=(19071,)))
    C.kc("apply", "-f", "-", stdin=sts("vct", 1, FRONT, "services", ports=(8080,)))
    C.kc("apply", "-f", "-", stdin=sts("vcn", n, C.NODE, "services", C.CPU, C.MEM, ports=(19107,)))
    t0 = time.time()
    while time.time() - t0 < 900:
        ps = C.pods("sys=vespa")
        if len(ps) == n + 2 and all(ph == "Running" for _, _, _, ph, _ in ps):
            break
        time.sleep(5)
    else:
        raise RuntimeError(f"vespa pods not running: {C.pods('sys=vespa')}")
    ip = {name: ip for name, ip, *_ in ps}
    cfg = ip["vcfg-0"]
    while time.time() - t0 < 1200:
        try:
            if requests.get(f"http://{cfg}:19071/state/v1/health", timeout=5).json()["status"]["code"] == "up":
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        raise RuntimeError("vespa config server never came up")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("services.xml", services(n))
        z.writestr("hosts.xml", hosts(n))
        z.writestr("schemas/vec.sd", SCHEMA)
    r = requests.post(f"http://{cfg}:19071/application/v2/tenant/default/prepareandactivate",
                      data=buf.getvalue(), headers={"Content-Type": "application/zip"}, timeout=900)
    if r.status_code != 200:
        raise RuntimeError(f"deploy failed {r.status_code}: {r.text[:800]}")
    front = ip["vct-0"]
    while time.time() - t0 < 1800:
        try:
            j = requests.get(f"http://{front}:8080/search/",
                             params={"yql": "select * from vec where true", "hits": 0},
                             timeout=10).json()
            if "root" in j and "errors" not in j["root"]:
                return ps, front
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError("vespa container never served")


def feed(front, base):
    s = requests.Session()
    s.mount("http://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=96))

    def put(i):
        url = f"http://{front}:8080/document/v1/sqn/vec/docid/{i}"
        body = {"fields": {"doc_id": int(i), "vector": {"values": base[i].tolist()}}}
        for _ in range(20):
            try:
                if s.post(url, json=body, timeout=120).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError(f"feed failed at {i}")
    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        for _ in ex.map(put, range(len(base)), chunksize=256):
            pass
    t0 = time.time()
    while time.time() - t0 < 3600:
        j = requests.get(f"http://{front}:8080/search/",
                         params={"yql": "select * from vec where true", "hits": 0}, timeout=60).json()
        if j["root"]["fields"]["totalCount"] == len(base):
            return
        time.sleep(10)
    raise RuntimeError("vespa doc count never reached the corpus size")


def searcher(front, with_vec):
    s = requests.Session()
    # select * so presentation.summary decides the returned fields
    # ("select doc_id" would strip the vector from the withvec summary)
    yql = (f"select * from vec where {{targetHits:{C.TOPK}, "
           f"hnsw.exploreAdditionalHits:{C.EF - C.TOPK}}}nearestNeighbor(vector,q)")
    summ = "withvec" if with_vec else "ids"

    def q(v):
        body = {"yql": yql, "input.query(q)": {"values": v.tolist()}, "ranking": "ann",
                "hits": C.TOPK, "presentation.summary": summ, "timeout": "10s"}
        j = s.post(f"http://{front}:8080/search/", json=body, timeout=30).json()
        return [h["fields"]["doc_id"] for h in j["root"].get("children", [])]
    return q


def main():
    base, qv, gt = C.dataset()
    C.kc("create", "namespace", C.NS, check=False)
    import sys
    for n in ([int(sys.argv[1])] if len(sys.argv) > 1 else (1, 2, 4, 8, 16)):
        labels = [f"Q_{t}_N{n}_r{r}" for r in range(1, C.REPS + 1) for t in ("ID", "VEC")]
        if set(labels) <= C.done_labels(CSV):
            continue
        C.log(f"== vespa content nodes={n}")
        ps, front = deploy(n)
        t0 = time.time()
        feed(front, base)
        C.log(f"   fed + visible in {time.time()-t0:.0f}s")
        path = [p[0] for p in ps if not p[0].startswith("vcfg")]
        done = C.done_labels(CSV)
        for r in range(1, C.REPS + 1):
            for t in ("ID", "VEC"):
                lab = f"Q_{t}_N{n}_r{r}"
                if lab in done:
                    continue
                C.run_row(lab, searcher(front, t == "VEC"), qv, gt, path,
                          dict(system="vespa", family="Q", mode=t, shards="", nodes=n, rep=r,
                               segs_per_shard="",
                               placement="flat"), CSV)
    C.delete_selector("sys=vespa")
    C.log("=== vespa sweep done ===")


if __name__ == "__main__":
    main()
