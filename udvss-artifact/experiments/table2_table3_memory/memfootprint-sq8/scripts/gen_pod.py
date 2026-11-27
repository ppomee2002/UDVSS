#!/usr/bin/env python3
"""Emit the k8s manifest for one engine pod of the memory-footprint experiment.

Every engine instance runs as ONE pod on node milvus-stream110 so that the pod's
cgroup holds the whole engine (Milvus standalone with embedded etcd + local
storage; Qdrant; Vespa config server + container + proton) and nothing else.
The memory limit is the per-pod cgroup memory.max -- no host-global setting is
touched.  CPU is held at 8 cores for every engine (CFS quota); because the pod
still *sees* 208 cores, thread pools are pinned to 8 explicitly where the engine
allows it (Qdrant env, Vespa proton hwinfo override in services.xml).

usage: gen_pod.py <milvus|qdrant|vespa> <name> <mem_limit e.g. 32Gi> [restartPolicy]
"""
import sys

NODE = "milvus-stream110"
NS = "memexp"
CPU = "8"

IMAGES = {
    "milvus": "milvusdb/milvus:v2.6.23",      # same as the existing milvus-bench / k3s cluster
    "qdrant": "qdrant/qdrant:v1.19.1",        # same version as the running qb-qdrant
    "vespa":  "vespaengine/vespa:8.754.14",   # same version as the running vb-vespa
}

def pod(system, name, mem, restart="Never"):
    head = f"""apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: {NS}
  labels: {{app: memexp, system: {system}}}
spec:
  nodeSelector: {{kubernetes.io/hostname: {NODE}}}
  restartPolicy: {restart}
  terminationGracePeriodSeconds: 10
"""
    res = f"""    resources:
      requests: {{cpu: "{CPU}", memory: "2Gi"}}
      limits:   {{cpu: "{CPU}", memory: "{mem}"}}
"""
    if system == "milvus":
        return head + f"""  volumes:
  - name: data
    emptyDir: {{}}
  - name: cfg
    configMap: {{name: memexp-milvus-cfg}}
  containers:
  - name: engine
    image: {IMAGES[system]}
    command: ["milvus", "run", "standalone"]
    securityContext: {{seccompProfile: {{type: Unconfined}}}}
    env:
    - {{name: ETCD_USE_EMBED, value: "true"}}
    - {{name: ETCD_DATA_DIR, value: /var/lib/milvus/etcd}}
    - {{name: ETCD_CONFIG_PATH, value: /milvus/configs/embedEtcd.yaml}}
    - {{name: COMMON_STORAGETYPE, value: local}}
    - {{name: DEPLOY_MODE, value: STANDALONE}}
    volumeMounts:
    - {{name: data, mountPath: /var/lib/milvus}}
    - {{name: cfg, mountPath: /milvus/configs/embedEtcd.yaml, subPath: embedEtcd.yaml}}
    - {{name: cfg, mountPath: /milvus/configs/user.yaml, subPath: user.yaml}}
    ports: [{{containerPort: 19530}}, {{containerPort: 9091}}]
""" + res
    if system == "qdrant":
        return head + f"""  volumes:
  - name: data
    emptyDir: {{}}
  containers:
  - name: engine
    image: {IMAGES[system]}
    env:
    - {{name: QDRANT__STORAGE__PERFORMANCE__MAX_SEARCH_THREADS, value: "8"}}
    - {{name: QDRANT__STORAGE__PERFORMANCE__MAX_OPTIMIZATION_THREADS, value: "8"}}
    - {{name: QDRANT__TELEMETRY_DISABLED, value: "true"}}
    volumeMounts:
    - {{name: data, mountPath: /qdrant/storage}}
    ports: [{{containerPort: 6333}}, {{containerPort: 6334}}]
""" + res
    if system == "vespa":
        # vespa-start-configserver insists that the local hostname equals
        # VESPA_CONFIGSERVERS, so the pod hostname is pinned to the pod name.
        return head + f"""  hostname: {name}
  volumes:
  - name: data
    emptyDir: {{}}
  containers:
  - name: engine
    image: {IMAGES[system]}
    env:
    - {{name: VESPA_CONFIGSERVERS, value: {name}}}
    volumeMounts:
    - {{name: data, mountPath: /opt/vespa/var}}
    ports: [{{containerPort: 8080}}, {{containerPort: 19071}}]
""" + res
    raise SystemExit(f"unknown system {system}")

if __name__ == "__main__":
    print(pod(sys.argv[1], sys.argv[2], sys.argv[3], *(sys.argv[4:5] or [])))
