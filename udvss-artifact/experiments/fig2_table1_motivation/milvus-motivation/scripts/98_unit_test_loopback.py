"""Offline check of the loopback arm plumbing (no cluster needed).

  1. 18_render_loopback.build() on Deployments shaped like chart 5.0.28's render
  2. list_pods() expands the shared pod into streamingnode + querynode processes
  3. podnet reads a shared netns ONCE and keeps eth0 / lo apart

Run: python3 scripts/98_unit_test_loopback.py
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("render", os.path.join(HERE, "18_render_loopback.py"))
render = importlib.util.module_from_spec(spec); spec.loader.exec_module(render)
from lib import common, podnet  # noqa: E402

fail = []
def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond: fail.append(msg)


def chart_deploy(comp, port, extra_vol):
    """Mirrors templates/{streamingnode,querynode}-deployment.yaml of chart 5.0.28."""
    return {"spec": {
        "selector": {"matchLabels": {"app.kubernetes.io/name": "milvus",
                                     "app.kubernetes.io/instance": "milvus", "component": comp}},
        "template": {"spec": {
            "serviceAccountName": "default",
            "containers": [{
                "name": comp, "image": "milvusdb/milvus:v2.6.23",
                "args": ["milvus", "run", comp],
                "env": [{"name": "LOCAL_STORAGE_SIZE", "value": "10"}] if comp == "querynode" else [],
                "ports": [{"name": comp, "containerPort": port, "protocol": "TCP"},
                          {"name": "metrics", "containerPort": 9091, "protocol": "TCP"}],
                "livenessProbe": {"tcpSocket": {"port": "metrics"}},
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": "metrics"}},
                "resources": {"requests": {"cpu": "4"}, "limits": {"cpu": "4"}},
                "volumeMounts": [{"name": "milvus-config", "mountPath": "/milvus/configs/user.yaml"},
                                 {"name": "tools", "mountPath": "/milvus/tools"},
                                 {"name": extra_vol, "mountPath": "/var/lib/milvus"}]}],
            "volumes": [{"name": "milvus-config", "configMap": {"name": "milvus"}},
                        {"name": "tools", "emptyDir": {}},
                        {"name": extra_vol, "emptyDir": {}}],
            "affinity": {"podAntiAffinity": {}},
            "nodeSelector": {"milvus-role": "streaming" if comp == "streamingnode" else "query"},
        }}}}


print("== 1. render ==")
FQDN = "qn-loop.milvus.svc.cluster.local"
svc, dep = render.build(chart_deploy("streamingnode", 22222, "woodpecker"),
                        chart_deploy("querynode", 21123, "disk"),
                        ns="milvus", release="milvus", fqdn=FQDN, qn_port=21133,
                        qn_metrics_port=9092, node_role="query")
pod = dep["spec"]["template"]["spec"]
sn, qn = pod["containers"]
env_q = {e["name"]: e["value"] for e in qn["env"]}
env_s = {e["name"] for e in sn.get("env", [])}
check([c["name"] for c in pod["containers"]] == ["streamingnode", "querynode"], "two containers, one pod")
check(env_q.get("QUERYNODE_IP") == FQDN and env_q.get("QUERYNODE_PORT") == "21133"
      and env_q.get("METRICS_PORT") == "9092", "worker QN env: ip=FQDN port=21133 metrics=9092")
check(not ({"QUERYNODE_IP", "QUERYNODE_PORT", "METRICS_PORT"} & env_s),
      "streamingnode (embedded QN) env untouched -> advertises pod IP:21123, metrics 9091")
check(pod["hostAliases"] == [{"ip": "127.0.0.1", "hostnames": [FQDN]}], "hostAliases FQDN -> 127.0.0.1")
check(pod["nodeSelector"] == {"milvus-role": "query"}, "pinned to worker node (109)")
check("affinity" not in pod, "chart anti-affinity dropped")
names = [p["name"] for c in pod["containers"] for p in c["ports"]]
check(len(names) == len(set(names)) and all(len(n) <= 15 for n in names), f"port names unique, <=15: {names}")
cports = [p["containerPort"] for c in pod["containers"] for p in c["ports"]]
check(len(cports) == len(set(cports)), f"container ports unique: {cports}")
vols = [v["name"] for v in pod["volumes"]]
check(len(vols) == len(set(vols)), f"volume names unique: {vols}")
mounts_ok = all(m["name"] in vols for c in pod["containers"] for m in c["volumeMounts"])
check(mounts_ok, "every volumeMount resolves")
sn_data = next(m["name"] for m in sn["volumeMounts"] if m["mountPath"] == "/var/lib/milvus")
qn_data = next(m["name"] for m in qn["volumeMounts"] if m["mountPath"] == "/var/lib/milvus")
check(sn_data != qn_data, "separate /var/lib/milvus (runQueryNode wipes its local dir at start)")
check(qn["readinessProbe"]["httpGet"]["port"] == "qn-metrics" and qn["livenessProbe"]["tcpSocket"]["port"] == "qn-metrics",
      "worker probes -> its own metrics port")
check(sn["readinessProbe"]["httpGet"]["port"] == "sn-metrics", "streamingnode probes -> its own metrics port")
check(qn["resources"] == {"requests": {"cpu": "4"}, "limits": {"cpu": "4"}}, "worker resources copied verbatim")
check(svc["spec"]["clusterIP"] == "None" and svc["spec"]["publishNotReadyAddresses"] is True,
      "headless Service, publishes not-ready address")
check(svc["spec"]["selector"] == dep["spec"]["selector"]["matchLabels"], "Service selects exactly this pod")
check(dep["spec"]["template"]["metadata"]["labels"]["component"] == common.LOOP_COMPONENT,
      "pod label component=snqn-loop (not picked up by chart Services)")

print("== 2. list_pods expansion ==")
fake = {"items": [
    {"metadata": {"name": "milvus-proxy-a", "labels": {"component": "proxy"}},
     "spec": {"nodeName": "p94"}, "status": {"phase": "Running", "podIP": "10.0.0.1"}},
    {"metadata": {"name": "milvus-snqn-loop-x", "labels": {"component": "snqn-loop"}},
     "spec": {"nodeName": "q109"}, "status": {"phase": "Running", "podIP": "10.0.2.9"}},
    {"metadata": {"name": "milvus-querynode-old", "labels": {"component": "querynode"},
                  "deletionTimestamp": "2026-09-23T00:00:00Z"},
     "spec": {"nodeName": "q109"}, "status": {"phase": "Running", "podIP": "10.0.2.3"}},
]}
common.kubectl = lambda *a, **k: json.dumps(fake)
pods = common.list_pods()
by = {p.component: p for p in pods}
check(sorted(by) == ["proxy", "querynode", "streamingnode"], f"components: {sorted(by)}")
check(by["querynode"].metrics_url == "http://10.0.2.9:9092/metrics", "worker metrics url on 9092")
check(by["streamingnode"].metrics_url == "http://10.0.2.9:9091/metrics", "delegator metrics url on 9091")
check(by["querynode"].netns_key == by["streamingnode"].netns_key == "milvus-snqn-loop-x", "shared netns key")
check(len({p.name for p in pods}) == len(pods), "process names unique")
check(common.net_group(pods) == {"milvus-proxy-a": "proxy", "milvus-snqn-loop-x": "snqn"}, "net groups")

print("== 3. podnet ==")
DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: {lo} 10 0 0 0 0 0 0 {lo} 10 0 0 0 0 0 0
  eth0: {rx} 20 0 0 0 0 0 0 {tx} 20 0 0 0 0 0 0
"""
calls = []
state = {"t": 0}
def fake_read(p, container=None):
    calls.append(p.pod)
    k = state["t"]
    return podnet.parse_proc_net_dev(DEV.format(lo=1000 + k * 500_000, rx=5000 + k * 10_000, tx=6000 + k * 20_000))
podnet.read_pod = fake_read
a = podnet.NetSnapshot.take(pods)
check(calls.count("milvus-snqn-loop-x") == 1, "shared netns read once, not once per process")
state["t"] = 1
b = podnet.NetSnapshot.take(pods)
eth = podnet.per_query(podnet.delta(a, b), None, 100, 1.0)
lo = podnet.per_query(podnet.delta(a, b, "lo"), None, 100, 1.0, iface="lo")
check(eth["milvus-snqn-loop-x"]["rx_bytes_per_query"] == 100.0, "eth0 rx per query")
check(lo["milvus-snqn-loop-x"]["tx_bytes_per_query"] == 5000.0, "lo per query kept separate from eth0")
idle = podnet.idle_rate_from(a, b)
check(set(idle["milvus-snqn-loop-x"]) == {"rx_bytes_per_s", "tx_bytes_per_s", "lo_rx_bytes_per_s", "lo_tx_bytes_per_s"},
      "idle baseline carries eth0 and lo")
lo2 = podnet.per_query(podnet.delta(a, b, "lo"), idle, 100, 1.0, iface="lo")
check(lo2["milvus-snqn-loop-x"]["tx_bytes_per_query"] == 0.0, "lo idle subtraction uses lo keys")

print()
print("LOOPBACK UNIT TEST FAILED" if fail else "LOOPBACK UNIT TEST OK")
sys.exit(1 if fail else 0)
