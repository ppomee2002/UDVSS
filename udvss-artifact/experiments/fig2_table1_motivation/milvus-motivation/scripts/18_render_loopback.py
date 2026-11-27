#!/usr/bin/env python3
"""Render the loopback arm: StreamingNode + worker QueryNode as ONE pod, plus its
headless Service. See docs/07_loopback_ab_plan.md.

Read-only against the cluster: it only `kubectl get`s the chart's own rendered
streamingnode and querynode Deployments and writes a manifest file. Applying it is
19_loopback_ab.sh's job.

Why derive from the live Deployments instead of a hand-written pod: the remote arms
run exactly those pod specs. Copying image, args, config mounts, env and resources
from them is the only way to guarantee the loopback arm differs in transport and
nothing else.

What is changed, and nothing more:
  * both containers go into one pod (one netns); volumes/ports get sn-/qn- prefixes
    because names must be unique within a pod
  * the worker QueryNode advertises QUERYNODE_IP=<FQDN of the headless Service> and
    listens on QUERYNODE_PORT (21123 is taken by the StreamingNode's embedded QN)
  * the worker's metrics/healthz move to METRICS_PORT (9091 is taken likewise)
  * hostAliases maps the FQDN to 127.0.0.1 inside this pod only; every other pod
    resolves it through cluster DNS to this pod's IP
  * nodeSelector pins the pod to the worker's node (segcore CPU unchanged)

Usage:
  18_render_loopback.py --out results/loopback/snqn-loop.yaml
  18_render_loopback.py --sn-json sn.json --qn-json qn.json --out x.yaml   # offline
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from typing import Any, Dict, List

import yaml

NAME = "snqn-loop"
SVC = "qn-loop"
LOOP_LABEL = {"mm-arm": "loopback"}


def kget_deploy(ns: str, release: str, component: str) -> Dict[str, Any]:
    out = subprocess.run(
        ["kubectl", "-n", ns, "get", "deploy", "-l",
         f"app.kubernetes.io/instance={release},component={component}", "-o", "json"],
        capture_output=True, text=True, check=True).stdout
    items = json.loads(out).get("items", [])
    if len(items) != 1:
        sys.exit(f"expected exactly 1 {component} Deployment for release {release}, got {len(items)}")
    return items[0]


def _rename_named_port(probe: Dict[str, Any] | None, mapping: Dict[str, Any]) -> None:
    if not probe:
        return
    for kind in ("httpGet", "tcpSocket", "grpc"):
        h = probe.get(kind)
        if h and "port" in h and h["port"] in mapping:
            h["port"] = mapping[h["port"]]


def _prefix_container(c: Dict[str, Any], pre: str, vol_map: Dict[str, str]) -> None:
    for m in c.get("volumeMounts", []) or []:
        m["name"] = vol_map[m["name"]]


def _env_set(c: Dict[str, Any], name: str, value: str) -> None:
    env = c.setdefault("env", [])
    env[:] = [e for e in env if e.get("name") != name]
    env.append({"name": name, "value": value})


def _env_names(c: Dict[str, Any]) -> List[str]:
    return [e.get("name", "") for e in c.get("env", []) or []]


def build(sn: Dict[str, Any], qn: Dict[str, Any], *, ns: str, release: str,
          fqdn: str, qn_port: int, qn_metrics_port: int, node_role: str) -> List[Dict[str, Any]]:
    sn_spec = copy.deepcopy(sn["spec"]["template"]["spec"])
    qn_spec = copy.deepcopy(qn["spec"]["template"]["spec"])
    match = dict(sn["spec"]["selector"]["matchLabels"])
    match.pop("component", None)

    sn_c = sn_spec["containers"][0]
    qn_c = qn_spec["containers"][0]
    sn_c["name"], qn_c["name"] = "streamingnode", "querynode"

    # The StreamingNode process hosts the delegator in an embedded QueryNode that
    # reads the SAME queryNode.ip/port keys (cmd/milvus/util.go: `run streamingnode`
    # sets EnableQueryNode=true). If it inherited the loopback name, the Proxy on
    # another machine would be told to dial the delegator at 127.0.0.1. Refuse.
    for k in ("QUERYNODE_IP", "QUERYNODE_PORT", "MILVUS_QUERYNODE_IP", "MILVUS_QUERYNODE_PORT"):
        if k in _env_names(sn_c):
            sys.exit(f"streamingnode already sets {k}; the embedded QN would inherit it")

    # ---- volumes: prefix, because the two specs both define e.g. milvus-config
    volumes: List[Dict[str, Any]] = []
    for pre, spec in (("sn-", sn_spec), ("qn-", qn_spec)):
        vmap = {v["name"]: pre + v["name"] for v in spec.get("volumes", []) or []}
        for v in spec.get("volumes", []) or []:
            v = copy.deepcopy(v)
            v["name"] = vmap[v["name"]]
            volumes.append(v)
        for c in spec["containers"] + (spec.get("initContainers") or []):
            _prefix_container(c, pre, vmap)
        for ic in spec.get("initContainers") or []:
            ic["name"] = pre + ic["name"]

    # ---- ports: names must be unique across the pod and <= 15 chars
    def fix_ports(c: Dict[str, Any], pre: str, renum: Dict[int, int]) -> Dict[str, Any]:
        name_map: Dict[str, Any] = {}
        for p in c.get("ports", []) or []:
            old_name = p.get("name")
            if p["containerPort"] in renum:
                p["containerPort"] = renum[p["containerPort"]]
            if old_name:
                p["name"] = (pre + old_name)[:15]
                name_map[old_name] = p["name"]
        return name_map

    sn_names = fix_ports(sn_c, "sn-", {})
    qn_names = fix_ports(qn_c, "qn-", {21123: qn_port, 9091: qn_metrics_port})
    for pr in ("livenessProbe", "readinessProbe", "startupProbe"):
        _rename_named_port(sn_c.get(pr), sn_names)
        _rename_named_port(qn_c.get(pr), qn_names)
        # a probe that hard-codes 9091 would hit the StreamingNode's healthz
        for kind in ("httpGet", "tcpSocket"):
            h = (qn_c.get(pr) or {}).get(kind)
            if h and h.get("port") == 9091:
                h["port"] = qn_metrics_port

    _env_set(qn_c, "QUERYNODE_IP", fqdn)
    _env_set(qn_c, "QUERYNODE_PORT", str(qn_port))
    _env_set(qn_c, "METRICS_PORT", str(qn_metrics_port))

    pod_spec: Dict[str, Any] = {
        "containers": [sn_c, qn_c],
        "volumes": volumes,
        "hostAliases": [{"ip": "127.0.0.1", "hostnames": [fqdn]}],
        "nodeSelector": {"milvus-role": node_role},
        "terminationGracePeriodSeconds": max(sn_spec.get("terminationGracePeriodSeconds", 30),
                                             qn_spec.get("terminationGracePeriodSeconds", 30)),
    }
    inits = (sn_spec.get("initContainers") or []) + (qn_spec.get("initContainers") or [])
    if inits:
        pod_spec["initContainers"] = inits
    for k in ("serviceAccountName", "imagePullSecrets", "securityContext", "tolerations",
              "dnsPolicy", "restartPolicy", "schedulerName"):
        if k in sn_spec:
            pod_spec[k] = sn_spec[k]
    # affinity / topologySpreadConstraints are deliberately NOT copied: the chart's
    # anti-affinity is written against component=querynode|streamingnode, and the
    # nodeSelector above already fixes the node.

    labels = {**match, "component": NAME, **LOOP_LABEL}
    deploy = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": f"{release}-{NAME}", "namespace": ns, "labels": labels,
                     "annotations": {"mm/purpose": "loopback A/B, docs/07_loopback_ab_plan.md"}},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {**match, **LOOP_LABEL}},
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }
    svc = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": SVC, "namespace": ns, "labels": {**match, **LOOP_LABEL}},
        "spec": {
            "clusterIP": "None",
            # QueryCoord dials the worker as soon as its session appears in etcd,
            # which is before the pod turns Ready. Without this the name has no A
            # record in that window and the first LoadSegments fails.
            "publishNotReadyAddresses": True,
            "selector": {**match, **LOOP_LABEL},
            "ports": [{"name": "grpc-qn", "port": qn_port, "targetPort": qn_port}],
        },
    }
    return [svc, deploy]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", default=os.environ.get("MM_NS", "milvus"))
    ap.add_argument("--release", default=os.environ.get("MM_RELEASE", "milvus"))
    ap.add_argument("--cluster-domain", default=os.environ.get("MM_CLUSTER_DOMAIN", "cluster.local"))
    ap.add_argument("--qn-port", type=int, default=int(os.environ.get("MM_LOOP_QN_PORT", 21133)))
    ap.add_argument("--qn-metrics-port", type=int, default=int(os.environ.get("MM_LOOP_QN_METRICS_PORT", 9092)))
    ap.add_argument("--node-role", default="query", help="milvus-role label of the worker's node (109)")
    ap.add_argument("--sn-json"); ap.add_argument("--qn-json")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    if a.sn_json and a.qn_json:
        sn, qn = json.load(open(a.sn_json)), json.load(open(a.qn_json))
    else:
        sn = kget_deploy(a.ns, a.release, "streamingnode")
        qn = kget_deploy(a.ns, a.release, "querynode")
    fqdn = f"{SVC}.{a.ns}.svc.{a.cluster_domain}"
    docs = build(sn, qn, ns=a.ns, release=a.release, fqdn=fqdn, qn_port=a.qn_port,
                 qn_metrics_port=a.qn_metrics_port, node_role=a.node_role)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as fh:
        yaml.safe_dump_all(docs, fh, sort_keys=False)
    print(f"wrote {a.out}")
    print(f"  worker QN advertises {fqdn}:{a.qn_port}  (127.0.0.1 inside the pod via hostAliases)")
    print(f"  worker metrics/healthz :{a.qn_metrics_port}   node milvus-role={a.node_role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
