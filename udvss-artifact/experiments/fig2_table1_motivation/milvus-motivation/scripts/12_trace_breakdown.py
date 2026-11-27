#!/usr/bin/env python3
"""Table 3, precise path. Build the latency breakdown from OpenTelemetry spans
instead of Prometheus histograms.

Why this exists: three of the Milvus histograms that Table 3 would otherwise use
(`milvus_proxy_sq_latency`, `milvus_proxy_sq_wait_result_latency`,
`milvus_querynode_sq_req_latency`) are observed with `.Milliseconds()` in v2.6.23 --
integer milliseconds. At the ~3 ms latencies measured here that is a +-1 ms
quantisation on the largest columns. Spans carry microseconds.

Reads the Jaeger query API (all-in-one, in-memory storage):
    GET /api/services
    GET /api/traces?service=<svc>&operation=<op>&limit=<n>&lookback=<w>

It reports TWO different decompositions, because they answer different questions and
mixing them up is the easy mistake here:

  (a) WORK per query -- sum over all spans of a kind of their SELF time
      (duration minus the union of the child intervals). Sibling spans run in
      PARALLEL, so this sum is much larger than the query's latency: at S=4, N=4
      the 16 SearchSegments spans contributed 9150us of self time inside a 2802us
      query. Use this for "where does the cluster spend CPU".

  (b) CRITICAL PATH per query -- the chain the latency actually waits on.
      Children are a MIX of sequential and parallel here (Proxy-Search-PreExecute ->
      -Execute -> -PostExecute run in sequence, while the S shard RPCs and the N
      worker RPCs fan out in parallel), so neither "sum the children" nor "take the
      max child" is right on its own. We cluster a node's children into maximal
      OVERLAPPING groups: within a group only the longest path counts (parallel),
      across groups the groups add (sequential). A node is charged
      `duration - union(children intervals)` as its own time. These contributions
      sum to the root duration. **This is Table 3.**

    12_trace_breakdown.py --jaeger http://10.42.0.11:16686 --limit 300
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.common import percentile, write_csv, write_json


def api(base: str, path: str, **params) -> dict:
    """Jaeger all-in-one keeps traces in RAM and WILL die under a large pull.

    Observed 2026-09-21: at S=16 a single Search trace carries hundreds of spans
    (16 shards x N workers x segments); asking for 300 of them OOMKilled the pod
    (exitCode 137, 8Gi limit) and the fetch surfaced only as
    `http.client.RemoteDisconnected`, losing two conditions silently. So retry,
    and halve the requested limit each time -- a smaller sample of a condition is
    worth far more than no sample at all.
    """
    url = f"{base.rstrip('/')}{path}"
    attempts = int(os.environ.get("JAEGER_ATTEMPTS", 5))
    for i in range(attempts):
        p = dict(params)
        if i and "limit" in p:
            p["limit"] = max(20, int(p["limit"]) // (2 ** i))
        u = url + ("?" + urllib.parse.urlencode(p) if p else "")
        try:
            with urllib.request.urlopen(u, timeout=120) as r:
                return json.loads(r.read().decode())
        except Exception as exc:
            if i == attempts - 1:
                raise
            wait = 10 * (i + 1)
            print(f"  jaeger fetch failed ({type(exc).__name__}: {exc}); "
                  f"retry {i+1}/{attempts-1} in {wait}s"
                  + (f" with limit={p.get('limit')}" if "limit" in p else ""),
                  flush=True)
            time.sleep(wait)


def covered(sp, children, svc_of, _memo=None) -> float:
    """Wall time this span's subtree accounts for, measured in the span's OWN clock.

    The hard constraint: timestamps from different services are NOT comparable.
    proxy and streamingnode run separate clocks, so any interval arithmetic that
    mixes them is unsound. Three formulas were tried and all failed on this data,
    each in a different direction (accounted critical path / root duration):

      direct children, no clip ................ 102-117%  (misses grandchildren:
          segcore hangs under `schedule`, which reports 16us while its children run
          800us+, so SearchSegments kept 1139us of "self" that was really the ANN
          search -- then charged again on the way down)
      all descendants, clipped to parent ...... 173-203%  (clipping a child to its
          parent's window across a service boundary cuts most of it away)
      direct children widened to subtree ...... 42-56%   (the widened extent spans
          two clocks, so it over-subtracts; the proxy->delegator hop went to 0.0us)

    So the rule here splits on the service boundary:
      * children in the SAME service share a clock -> interval union, each widened
        to what its own subtree covers (fixes the `schedule` case).
      * a child in a DIFFERENT service is an RPC. Only its LENGTH is meaningful, so
        it is subtracted as a scalar; the remainder is network + serialisation.
        Parallel RPCs overlap, so take the max, never the sum.
    """
    if _memo is None:
        _memo = {}
    sid = sp["spanID"]
    if sid in _memo:
        return _memo[sid]
    own_svc = svc_of(sp)
    same, cross = [], []
    for k in children.get(sid, []):
        (same if svc_of(k) == own_svc else cross).append(k)
    ivs = []
    for k in same:
        a = float(k["startTime"])
        ivs.append((a, a + max(float(k["duration"]), covered(k, children, svc_of, _memo))))
    u = union_length(ivs)
    c = 0.0
    for k in cross:
        c = max(c, max(float(k["duration"]), covered(k, children, svc_of, _memo)))
    _memo[sid] = max(u, c)
    return _memo[sid]


def child_cover(sp, children, svc_of=None) -> float:
    return covered(sp, children, svc_of or (lambda x: x.get("processID")))


def clusters(spans: List[dict]) -> List[List[dict]]:
    """Group spans into maximal sets that overlap in time (transitively).

    Within a cluster the spans ran concurrently, so only the longest one is on the
    critical path. Separate clusters ran one after another, so they add up.
    """
    if not spans:
        return []
    out: List[List[dict]] = []
    cur: List[dict] = []
    cur_end = -1
    for sp in sorted(spans, key=lambda x: x["startTime"]):
        s0, e0 = sp["startTime"], sp["startTime"] + sp["duration"]
        if cur and s0 < cur_end:
            cur.append(sp)
            cur_end = max(cur_end, e0)
        else:
            if cur:
                out.append(cur)
            cur, cur_end = [sp], e0
    out.append(cur)
    return out


def union_length(intervals: List[Tuple[int, int]]) -> int:
    """Total length covered by the union of [start, end) intervals, in the same unit."""
    if not intervals:
        return 0
    iv = sorted(intervals)
    total = 0
    cur_s, cur_e = iv[0]
    for s, e in iv[1:]:
        if s > cur_e:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    total += cur_e - cur_s
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jaeger", default="", help="e.g. http://10.42.0.11:16686")
    ap.add_argument("--service", default=None, help="root service (default: auto-detect a proxy-ish one)")
    ap.add_argument("--operation", default="milvus.proto.milvus.MilvusService/Search",
                    help="root operation to filter on; the default keeps health-check and "
                         "metrics RPCs (GetComponentStates, SetRates, GetProxyMetrics) out "
                         "of the table. Pass '' to disable the filter.")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--lookback", default="1h")
    ap.add_argument("--label", default="TRACE")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--from-raw", default=None,
                    help="recompute from a saved <label>_raw.json instead of querying "
                         "Jaeger. Lets the decomposition be re-derived after a formula "
                         "change without re-running the benchmark.")
    ap.add_argument("--save-raw", action="store_true",
                    help="also dump the fetched traces as <label>_raw.json, so the "
                         "decomposition can be recomputed offline instead of "
                         "re-running the benchmark")
    a = ap.parse_args()

    # --from-raw must not touch Jaeger at all: the point of it is to re-derive the
    # decomposition after a formula change, long after the ring buffer has rotated.
    if a.from_raw:
        traces = json.load(open(a.from_raw))
        print(f"loaded {len(traces)} traces from {a.from_raw}")
        svc = a.service
        if svc is None:
            for t in traces:
                procs = {pid: q.get("serviceName", "?")
                         for pid, q in (t.get("processes") or {}).items()}
                ids = {sp["spanID"] for sp in (t.get("spans") or [])}
                for sp in (t.get("spans") or []):
                    if not any(r.get("refType") == "CHILD_OF" and r.get("spanID") in ids
                               for r in (sp.get("references") or [])):
                        svc = procs.get(sp.get("processID"), "?")
                        break
                if svc:
                    break
        print(f"root service: {svc}")
    else:
        svcs = api(a.jaeger, "/api/services").get("data") or []
        print("services:", svcs)
        svc = a.service
        if svc is None:
            for cand in ("proxy", "milvus-proxy", "Proxy"):
                if cand in svcs:
                    svc = cand
                    break
        if svc is None:
            print("could not pick a root service; pass --service", file=sys.stderr)
            return 1
        print(f"root service: {svc}")
        ops = api(a.jaeger, "/api/operations", service=svc).get("data") or []
        print(f"operations on {svc}: {ops[:20]}{' ...' if len(ops) > 20 else ''}")
        q = {"service": svc, "limit": a.limit, "lookback": a.lookback}
        if a.operation:
            q["operation"] = a.operation
            print(f"root operation filter: {a.operation}")
        traces = api(a.jaeger, "/api/traces", **q).get("data") or []
        print(f"fetched {len(traces)} traces")

    if not traces:
        print("no traces -- is trace.exporter otlp and sampleFraction 1?", file=sys.stderr)
        return 1

    dur: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    self_: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    crit: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    crit_seen = 0
    roots: List[float] = []
    example_tree: List[str] = []
    example_crit: List[str] = []

    def root_of(t):
        spans = t.get("spans") or []
        ids = {sp["spanID"] for sp in spans}
        best = None
        for sp in spans:
            if not any(r.get("refType") == "CHILD_OF" and r.get("spanID") in ids
                       for r in (sp.get("references") or [])):
                if best is None or sp["startTime"] < best["startTime"]:
                    best = sp
        return best

    def has_error(t):
        return any((tg.get("key") == "error" and tg.get("value") in (True, "true"))
                   for sp in (t.get("spans") or []) for tg in (sp.get("tags") or []))

    # PRE-PASS. A traced window also catches Searches that FAILED fast: during the
    # load that follows a scale-up, "collection not loaded" returns in microseconds.
    # Observed 2026-09-21 at S=16 Q=1 -- root p50 286us against p95 4455us, a
    # bimodal mix that pulled the reported mean to 1859us while the client measured
    # 4874us. Those are real Search spans, just not queries, so they have to be
    # removed BEFORE any aggregation, not merely counted afterwards.
    dropped_error = sum(1 for t in traces if has_error(t))
    traces = [t for t in traces if not has_error(t)]
    dropped_tiny = 0
    _r = sorted(float(root_of(t).get("duration", 0)) for t in traces if root_of(t))
    if len(_r) >= 10:
        _med = _r[len(_r) // 2]
        _floor = float(os.environ.get("TRACE_MIN_FRAC", 0.2)) * _med
        before = len(traces)
        traces = [t for t in traces
                  if root_of(t) and float(root_of(t).get("duration", 0)) >= _floor]
        dropped_tiny = before - len(traces)
        if dropped_tiny:
            print(f"  dropped {dropped_tiny} trace(s) under {_floor:.0f}us "
                  f"(< {100*float(os.environ.get('TRACE_MIN_FRAC', 0.2)):.0f}% of median "
                  f"{_med:.0f}us) -- fast failures, not queries", flush=True)
    if dropped_error:
        print(f"  dropped {dropped_error} trace(s) carrying an error tag", flush=True)
    print(f"  {len(traces)} trace(s) used", flush=True)

    for t in traces:
        procs = {pid: p.get("serviceName", "?") for pid, p in (t.get("processes") or {}).items()}
        spans = t.get("spans") or []
        by_id = {s["spanID"]: s for s in spans}
        svc_of = lambda sp: procs.get(sp.get("processID"), "?")
        children: Dict[str, List[dict]] = defaultdict(list)
        root = None
        for s in spans:
            parent = None
            for ref in (s.get("references") or []):
                if ref.get("refType") == "CHILD_OF" and ref.get("spanID") in by_id:
                    parent = ref["spanID"]
                    break
            if parent is None:
                root = s if root is None or s["startTime"] < root["startTime"] else root
            else:
                children[parent].append(s)

        for s in spans:
            key = (procs.get(s.get("processID"), "?"), s.get("operationName", "?"))
            d = float(s.get("duration", 0))
            dur[key].append(d)
            self_[key].append(max(d - child_cover(s, children, svc_of), 0.0))

        if root is not None:
            roots.append(float(root.get("duration", 0)))
            # ---- critical path over a mixed sequential/parallel child set.
            crit_seen += 1
            chain: List[str] = []

            def cp_len(sp) -> float:
                kids = children.get(sp["spanID"], [])
                own = float(sp["duration"]) - child_cover(sp, children, svc_of)
                return max(own, 0.0) + sum(
                    max(cp_len(k) for k in cl) for cl in clusters(kids))

            def charge(sp, depth: int) -> None:
                kids = children.get(sp["spanID"], [])
                own = max(float(sp["duration"]) - child_cover(sp, children, svc_of), 0.0)
                key = (procs.get(sp.get("processID"), "?"), sp.get("operationName", "?"))
                crit[key].append(own)
                chain.append(f"{'  ' * depth}{key[0]}/{key[1]}  self +{own:.0f}us")
                for cl in clusters(kids):
                    winner = max(cl, key=cp_len)
                    charge(winner, depth + 1)

            charge(root, 0)
            if not example_crit:
                example_crit = list(chain)
            if not example_tree:
                def walk(s, depth):
                    example_tree.append(
                        f"{'  ' * depth}{procs.get(s.get('processID'),'?')}/"
                        f"{s.get('operationName')}  {s.get('duration')}us")
                    for c in sorted(children.get(s["spanID"], []), key=lambda x: x["startTime"]):
                        walk(c, depth + 1)
                walk(root, 0)

    print("\n=== one trace, as a tree ===")
    for line in example_tree[:40]:
        print("  " + line)

    rows = []
    for (service, op), ds in sorted(dur.items(), key=lambda kv: -sum(kv[1])):
        ss = sorted(ds)
        sf = sorted(self_[(service, op)])
        rows.append({
            "label": a.label, "service": service, "operation": op,
            "spans": len(ds),
            "spans_per_trace": round(len(ds) / len(traces), 3),
            "mean_us": round(sum(ss) / len(ss), 1),
            "p50_us": round(percentile(ss, 50), 1),
            "p95_us": round(percentile(ss, 95), 1),
            "p99_us": round(percentile(ss, 99), 1),
            "self_mean_us": round(sum(sf) / len(sf), 1),
            "self_total_per_trace_us": round(sum(sf) / len(traces), 1),
        })

    rt = sorted(roots)
    _mean = sum(rt) / len(rt) if rt else 0.0
    _med = rt[len(rt) // 2] if rt else 0.0
    bimodal = bool(_med and abs(_mean - _med) / _med > 0.25)
    if bimodal:
        print(f"  !! mean {_mean:.0f}us vs median {_med:.0f}us differ by "
              f"{100*abs(_mean-_med)/_med:.0f}% -- sample is not one population, "
              "do not trust these percentages", flush=True)
    print(f"\n=== root span (E2E at {svc}) over {len(rt)} traces ===")
    print(f"  mean {sum(rt)/len(rt):.1f}us  p50 {percentile(rt,50):.1f}  "
          f"p95 {percentile(rt,95):.1f}  p99 {percentile(rt,99):.1f}")

    print("\n=== per span, sorted by total time ===")
    hdr = f"  {'service':<16}{'operation':<34}{'/trace':>7}{'mean_us':>10}{'p95_us':>10}{'self_us':>10}{'self/trace':>12}"
    print(hdr)
    for r in rows[:25]:
        print(f"  {r['service']:<16}{r['operation']:<34}{r['spans_per_trace']:>7}"
              f"{r['mean_us']:>10}{r['p95_us']:>10}{r['self_mean_us']:>10}{r['self_total_per_trace_us']:>12}")

    work = sum(r["self_total_per_trace_us"] for r in rows)
    print(f"\n  (a) WORK: sum of self time per query = {work:.1f}us")
    print(f"      root span mean               = {sum(rt)/len(rt):.1f}us")
    print(f"      ratio {work/(sum(rt)/len(rt)):.2f}x -- this is PARALLELISM, not an error:")
    print("      sibling spans overlap in time, so total work exceeds wall-clock latency.")

    crows = []
    for (service, op), cs in crit.items():
        tot = sum(cs) / max(crit_seen, 1)
        crows.append({"label": a.label, "service": service, "operation": op,
                      "on_critical_path_per_trace": round(tot, 1),
                      "occurrences_per_trace": round(len(cs) / max(crit_seen, 1), 3)})
    crows.sort(key=lambda r: -r["on_critical_path_per_trace"])
    ctot = sum(r["on_critical_path_per_trace"] for r in crows)

    print(f"\n=== (b) CRITICAL PATH -- this is Table 3 ({crit_seen} traces) ===")
    print(f"  {'service':<16}{'operation':<46}{'us/query':>10}{'share':>8}")
    for r in crows:
        if r["on_critical_path_per_trace"] < 1.0:
            continue
        print(f"  {r['service']:<16}{r['operation']:<46}"
              f"{r['on_critical_path_per_trace']:>10.1f}{100*r['on_critical_path_per_trace']/ctot:>7.1f}%")
    print(f"  {'':<16}{'TOTAL':<46}{ctot:>10.1f}{100.0:>7.1f}%")
    rmean = sum(rt) / len(rt)
    print(f"  root span mean {rmean:.1f}us  (critical-path total {ctot:.1f}us, "
          f"{100*(ctot-rmean)/rmean:+.1f}%)")
    print("  The two are the same chain and would match exactly on a single clock. The")
    print("  residual is CLOCK SKEW between the three machines: a child span timestamped")
    print("  on another host can appear to start before or end after its parent, which")
    print("  perturbs `duration - union(children)`. Treat shares, not absolutes, as the")
    print("  result, and do not quote the total to better than this residual.")

    # ---- Table 3 shape: group the operations into the reporting stages.
    # A gRPC CLIENT span's self time is (client duration - server duration), i.e. the
    # hop: serialization + network + remote scheduling. That is why the two fan-out
    # rows below are the client spans and not the server ones.
    def stage(service: str, op: str) -> str:
        if service == "proxy" and op == "milvus.proto.query.QueryNode/Search":
            return "2. fan-out  proxy -> delegator (hop)"
        if service == "streamingnode" and op == "milvus.proto.query.QueryNode/SearchSegments":
            return "4. fan-out  delegator -> QueryNode (hop)"
        if service == "proxy":
            return "1. Proxy"
        if service == "streamingnode":
            return "3. Delegator (StreamingNode)"
        if service == "querynode":
            return "5. QueryNode local"
        if service == "segcore":
            return "6. segcore (vector search + reduce)"
        return f"7. other ({service})"

    # Finer split of "5. QueryNode local" + "6. segcore", so a slope can be
    # attributed to queueing vs the ANN kernel vs per-segment reduction rather
    # than to one undifferentiated block. Operation names are from the v2.6.23
    # spans actually observed on this cluster.
    QN_QUEUE = {"schedule"}
    SEG_SEARCH = {"SegCoreSearch", "PhyVectorSearchNode::Execute", "PhyMvccNode::Execute",
                  "ExecuteTask", "Task::Next"}

    def stage_fine(service: str, op: str) -> str:
        base = stage(service, op)
        if base == "5. QueryNode local":
            return "5a. QN queue" if op in QN_QUEUE else "5b. QN local other"
        if base == "6. segcore (vector search + reduce)":
            if op in SEG_SEARCH:
                return "6a. segcore ANN search"
            if op.startswith("ReduceHelper::") or op == "ReduceSearchResultsAndFillData":
                return "6b. segment reduce"
            return "6c. segcore other"
        return base

    groups: Dict[str, float] = defaultdict(float)
    groups_fine: Dict[str, float] = defaultdict(float)
    for r in crows:
        groups[stage(r["service"], r["operation"])] += r["on_critical_path_per_trace"]
        groups_fine[stage_fine(r["service"], r["operation"])] += r["on_critical_path_per_trace"]
    gtot = sum(groups.values())
    print(f"\n=== Table 3 (grouped critical path), {a.label} ===")
    print(f"  {'stage':<42}{'us/query':>10}{'share':>8}")
    for k in sorted(groups):
        print(f"  {k:<42}{groups[k]:>10.1f}{100*groups[k]/gtot:>7.1f}%")
    hop = sum(v for k, v in groups.items() if "fan-out" in k)
    print(f"  {'-> the two network hops together':<42}{hop:>10.1f}{100*hop/gtot:>7.1f}%")
    print(f"  {'-> actual vector search (segcore)':<42}{groups.get('6. segcore (vector search + reduce)',0):>10.1f}"
          f"{100*groups.get('6. segcore (vector search + reduce)',0)/gtot:>7.1f}%")

    print(f"\n=== fine-grained, {a.label} ===")
    ftot = sum(groups_fine.values())
    for k in sorted(groups_fine):
        print(f"  {k:<42}{groups_fine[k]:>10.1f}{100*groups_fine[k]/ftot:>7.1f}%")

    print("\n=== one trace's critical path ===")
    for line in example_crit:
        print("  " + line)
    accounted = ctot

    out_dir = a.out_dir or os.path.join(os.environ.get("MM_RESULTS", "."), "trace")
    if a.save_raw:
        write_json(os.path.join(out_dir, f"{a.label}_raw.json"), traces)
        print(f"  raw traces saved ({len(traces)})")
    write_csv(os.path.join(out_dir, f"{a.label}_spans.csv"), rows)
    write_csv(os.path.join(out_dir, f"{a.label}_critical_path.csv"), crows)
    write_json(os.path.join(out_dir, f"{a.label}_summary.json"), {
        "label": a.label, "jaeger": a.jaeger, "root_service": svc,
        "traces": len(traces),
        "traces_dropped_error": dropped_error,
        "traces_dropped_tiny": dropped_tiny,
        "bimodal": bimodal,
        "root_mean_us": sum(rt) / len(rt),
        "root_p50_us": percentile(rt, 50), "root_p95_us": percentile(rt, 95),
        "root_p99_us": percentile(rt, 99),
        "work_self_total_per_trace_us": work,
        "critical_path_total_us": accounted,
        "critical_path": crows,
        "critical_path_grouped": {k: round(v, 1) for k, v in sorted(groups.items())},
        "critical_path_grouped_fine": {k: round(v, 1) for k, v in sorted(groups_fine.items())},
        "example_critical_path": example_crit,
        "example_tree": example_tree[:60],
        "rows": rows,
    })
    print(f"\nwrote {out_dir}/{a.label}_spans.csv and _summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
