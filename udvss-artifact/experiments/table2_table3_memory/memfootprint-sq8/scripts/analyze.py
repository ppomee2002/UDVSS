#!/usr/bin/env python3
"""Aggregate every results/<tag>/<sys>-<mode>/rows.csv into
    results/all_rows.csv        every row of every series
    results/final_table.csv     one row per (system, mode, limit, N): the requested table
    results/SUMMARY.md          tables + reductions + capacity at fixed budgets
Nothing here is estimated unless the column / section says "fit" or "extrapolated".
"""
import csv, glob, json, os, sys
import numpy as np
ROOT = "/home/dev_path/experiments/memfootprint-sq8"
G = 1024 ** 3

def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

rows = []
for p in sorted(glob.glob(f"{ROOT}/results/p[0-9]*/*/rows.csv")):
    with open(p) as fh:
        rows += list(csv.DictReader(fh))
if not rows:
    sys.exit("no rows yet")
with open(f"{ROOT}/results/all_rows.csv", "w", newline="") as fh:
    names = []
    for r in rows:
        names += [k for k in r if k not in names]
    w = csv.DictWriter(fh, fieldnames=names, restval=""); w.writeheader(); w.writerows(rows)

outcomes = {}
for p in glob.glob(f"{ROOT}/results/p[0-9]*/*/outcome.json"):
    tag, sm = p.split("/")[-3], p.split("/")[-2]
    outcomes[(tag, sm)] = json.load(open(p))

# Milvus: prefer the post-hoc load-window recomputation where it exists
# (scripts/fix_milvus_pred.py); the live parse could miss segments of a load.
for p in glob.glob(f"{ROOT}/results/p[0-9]*/milvus-*/db_reported_fixed.jsonl"):
    tag, sm = p.split("/")[-3], p.split("/")[-2]
    fx = {o["N"]: o["m_load_pred_seg_mem_gb"] for o in map(json.loads, open(p))
          if o.get("m_load_pred_seg_mem_gb") is not None}
    for r in rows:
        if r["tag"] == tag and f"{r['system']}-{r['mode']}" == sm and r["phase"] in ("stable", "query") \
                and int(r["N"]) in fx and r.get("db_vec_index_gb") not in ("", None):
            r["db_vec_index_gb"] = str(fx[int(r["N"])])

# Milvus DB-reported memory, final rule: the querynode loader's per-row estimate
# is exactly linear in rows (checked on clean full loads), so the column is
# N x bytes_per_row, with bytes_per_row taken from the clean loads (fixed jsonl
# entries that cover a complete load).  The per-load log sums of the live runs
# could include compaction hand-offs or miss segments, so they are not used.
MILVUS_BPR = {}
for p in glob.glob(f"{ROOT}/results/p[0-9]*/milvus-*/db_reported_fixed.jsonl"):
    mode = p.split("/")[-2].split("-")[1]
    for o in map(json.loads, open(p)):
        if o.get("bytes_per_row") and o["N"] >= 1_000_000:
            MILVUS_BPR.setdefault(mode, []).append(o["bytes_per_row"])
MILVUS_BPR = {m: float(np.median(v)) for m, v in MILVUS_BPR.items()}
for r in rows:
    if r["system"] == "milvus" and r["mode"] in MILVUS_BPR and r["phase"] in ("stable", "query") \
            and r.get("db_vec_index_gb") not in ("", None):
        r["db_vec_index_gb"] = str(round(int(r["N"]) * MILVUS_BPR[r["mode"]] / G, 4))
        r["db_report_src"] = f"loader estimate {MILVUS_BPR[r['mode']]:.0f} B/row x N"

# ---- requested table ------------------------------------------------------------
key = lambda r: (r["system"], r["mode"], r["tag"], int(r["N"]))
by = {}
for r in rows:
    by.setdefault(key(r), {})[r["phase"]] = r
final = []
for k in sorted(by):
    ph = by[k]
    if k[3] == 0:
        continue
    st, q, b = ph.get("stable", {}), ph.get("query", {}), ph.get("build", {})
    peaks = [f(x.get("peak_cg_cur_gb")) for x in (b, q) if f(x.get("peak_cg_cur_gb")) is not None]
    peaks_rss = [f(x.get("peak_rss_gb")) for x in (b, q) if f(x.get("peak_rss_gb")) is not None]
    fail = b.get("failure") or q.get("failure") or ""
    o = outcomes.get((k[2], f"{k[0]}-{k[1]}")) or {}
    of = o.get("failure")
    # a failure recorded only in outcome.json (e.g. OOMKilled after the stable
    # sample, during recall/query) belongs to the last N that has rows
    if of and not fail and not q.get("qps") and k[3] == max(kk[3] for kk in by if kk[:3] == k[:3]):
        fail = of[0]
    mf = f(q.get("majfault_delta"))
    thrash = (mf / 30.0) if mf is not None else None
    final.append({
        "system": k[0], "representation": k[1].upper(), "limit": b.get("mem_limit") or st.get("mem_limit"),
        "tag": k[2], "N": k[3],
        "raw_vector_gb": f(b.get("raw_vec_gb_repr") or st.get("raw_vec_gb_repr")),
        "raw_fp32_gb": f(b.get("raw_vec_gb_fp32") or st.get("raw_vec_gb_fp32")),
        "db_index_vector_gb": f(st.get("db_vec_index_gb")),
        "process_rss_gb": f(st.get("rss_gb")),
        "process_rss_anon_gb": f(st.get("rss_anon_gb")),
        "pod_allprocs_rss_gb": f(st.get("rss_allprocs_gb")),
        "cgroup_gb": f(st.get("cg_cur_gb")),
        "cgroup_anon_gb": f(st.get("cg_anon_gb")),
        "peak_build_cgroup_gb": f(b.get("peak_cg_cur_gb")),
        "peak_query_cgroup_gb": f(q.get("peak_cg_cur_gb")),
        "peak_cgroup_gb": max(peaks) if peaks else None,
        "peak_rss_gb": max(peaks_rss) if peaks_rss else None,
        "qps": f(q.get("qps")), "p99_ms": f(q.get("lat_p99_ms")), "recall10": f(q.get("recall10")),
        "build_s": (f(b.get("insert_s")) or 0) + (f(b.get("settle_s")) or 0) if b.get("insert_s") else None,
        "majfault_per_s_query": round(thrash, 0) if thrash is not None else None,
        "pressure": ("thrashing" if thrash is not None and thrash > 1000 else ""),
        "failure": fail, "failure_msg": (b.get("failure_msg") or q.get("failure_msg") or "")[:160],
    })
# ---- Ceph rows (written to final_table.csv only; the pod-oriented sections below
# filter on the pod tags and do not use them) ------------------------------------
# One row per (run, representation, N, seastar cap, cache size), merged from the
# stable / query / failure rows of ceph/results/<run>/<arm>/rows.jsonl.
# Ceph has no cgroup: cgroup columns stay empty; process_rss = OSD RSS after the OSD
# is up (stable phase), db_index_vector_gb = seastore cache LRU during queries.
CEPH_RUNS = [  # (tag, result dirs, config note, load ran at the same cap+cache as the queries)
    ("ceph-smp8", ["main"], "smp8, SSD/segment device, OMAP on, cbjournal n/a; load at 24G cap, "
     "then OSD restarted per (cap, cache)", False),
    ("ceph-smp2-c1.5g", ["prof"], "smp2, RANDOM_BLOCK, cbjournal 1.5G, cache 1.5G, OMAP on, one OSD process", True),
    ("ceph-smp2-c500m", ["prof500"], "smp2, RANDOM_BLOCK, cbjournal 1.5G, cache 500M, OMAP on, one OSD process; "
     "steps run out of order (1M/2M first) so RSS is a high-water mark", True),
    ("ceph-smp2-cbj1.2g", ["prof12"], "smp2, RANDOM_BLOCK, cbjournal 1288486912, cache 500M, OMAP on, one OSD process", True),
    ("ceph-smp2-noomap", ["noomap", "noomap_ext"], "smp2, RANDOM_BLOCK, cbjournal 1288486912, cache 500M, "
     "OMAP OFF (CRIMSON_VECTOR_NO_OMAP_IDENTITY=1), one OSD process", True),
]
CEPH_EXTRA = ["cache_pershard", "seastar_alloc_gb", "cache_lru_gb", "cache_hit_ratio", "ceph_config"]

def ceph_final_rows():
    out = []
    for tag, dirs, note, load_same in CEPH_RUNS:
        for arm, rep in (("fp32", "FP32"), ("int8", "SQ8")):
            recs = []
            for d in dirs:
                p = f"{ROOT}/ceph/results/{d}/{arm}/rows.jsonl"
                if os.path.exists(p):
                    recs += [json.loads(l) for l in open(p)]
            loads = {r["N"]: r for r in recs if r["phase"] == "load"}
            groups = {}
            for r in recs:
                if r["phase"] == "load" and not r["failure"]:
                    continue
                cap = r["cap"] if r["phase"] != "load" else r["cap"]
                groups.setdefault((r["N"], cap, r.get("cache_pershard", "")), {})[r["phase"]] = r
            for (n, cap, cache), ph in sorted(groups.items(), key=lambda kv: (kv[0][0], -int(kv[0][1][:-1]), kv[0][2])):
                st, q, ld = ph.get("stable", {}), ph.get("query", {}), ph.get("load", {})
                fl = next((x for x in (ld, st, q) if x.get("failure")), {})
                lrow = loads.get(n, {}) if load_same else {}
                peaks = [f(x.get("peak_rss_gb")) for x in (st, q, lrow) if f(x.get("peak_rss_gb"))]
                ok_q = bool(q) and not q.get("failure")
                out.append({
                    "system": "ceph", "representation": rep, "limit": f"{cap}i", "tag": tag, "N": n,
                    "raw_vector_gb": f((q or st or ld or lrow).get("raw_vec_gb_repr")),
                    "raw_fp32_gb": f((q or st or ld or lrow).get("raw_vec_gb_fp32")),
                    "db_index_vector_gb": f(q.get("cache_lru_gb")) if ok_q else None,
                    "process_rss_gb": f(st.get("rss_gb")), "process_rss_anon_gb": f(st.get("rss_anon_gb")),
                    "pod_allprocs_rss_gb": None, "cgroup_gb": None, "cgroup_anon_gb": None,
                    "peak_build_cgroup_gb": None, "peak_query_cgroup_gb": None, "peak_cgroup_gb": None,
                    "peak_rss_gb": max(peaks) if peaks else None,
                    "qps": f(q.get("qps")) if ok_q else None,
                    "p99_ms": f(q.get("lat_p99_ms")) if ok_q else None,
                    "recall10": f(q.get("recall10")) if ok_q else None,
                    "build_s": f(lrow.get("load_s")) if lrow else None,
                    "majfault_per_s_query": None, "pressure": "",
                    "failure": fl.get("failure", ""), "failure_msg": (fl.get("failure_msg") or "")[:160],
                    "cache_pershard": cache,
                    "seastar_alloc_gb": f(q.get("seastar_alloc_gb")) if ok_q else f(st.get("seastar_alloc_gb")),
                    "cache_lru_gb": f(q.get("cache_lru_gb")) if ok_q else None,
                    "cache_hit_ratio": f(q.get("cache_hit_ratio")) if ok_q else None,
                    "ceph_config": note,
                })
    return out

ceph_rows_final = ceph_final_rows()
cols = list(final[0].keys()) + CEPH_EXTRA
with open(f"{ROOT}/results/final_table.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols, restval=""); w.writeheader(); w.writerows(final + ceph_rows_final)

def fmt(x, nd=2):
    if x is None or x == "":
        return "–"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)

md = ["# FP32 vs SQ8 memory footprint — results\n", f"Milvus loader bytes/row used for its DB column: {MILVUS_BPR}\n",
      "Generated by `scripts/analyze.py` from raw rows. GB = GiB (2^30). "
      "`pressure` = no hard failure but > 1000 major page faults/s in the cgroup during the query window "
      "(page-cache thrashing; only measured from p2 on). "
      "`RSS` = engine process (Vespa: proton only); `pod` = all processes in the pod; "
      "`cgroup` = pod memory.current (anon + page cache + kernel). Stable = after build/load, "
      "Peak = max over build and query windows.\n"]
for tag in sorted({r["tag"] for r in final}):
    md.append(f"\n## {tag}\n")
    md.append("| System | Repr | N | Raw vec GB | DB index/vec GB | Proc RSS GB (anon) | Pod RSS GB | cgroup GB | Peak cgroup GB (build/query) | QPS | p99 ms | recall@10 | Failure |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in [x for x in final if x["tag"] == tag]:
        md.append(f"| {r['system']} | {r['representation']} | {r['N']:,} | {fmt(r['raw_vector_gb'])} | "
                  f"{fmt(r['db_index_vector_gb'])} | {fmt(r['process_rss_gb'])} ({fmt(r['process_rss_anon_gb'])}) | "
                  f"{fmt(r['pod_allprocs_rss_gb'])} | {fmt(r['cgroup_gb'])} | "
                  f"{fmt(r['peak_build_cgroup_gb'])} / {fmt(r['peak_query_cgroup_gb'])} | {fmt(r['qps'],0)} | "
                  f"{fmt(r['p99_ms'],1)} | {fmt(r['recall10'],3)} | "
                  f"{r['failure'] or ('pressure: %s majflt/s' % int(r['majfault_per_s_query']) if r['pressure'] else 'no')} |")

# ---- reductions on the unconstrained scaling phase ---------------------------------
md.append("\n## FP32 → SQ8 reduction (p1-32g, unconstrained)\n")
md.append("Theoretical raw-vector reduction is 75 % (4×). `actual` = 1 − SQ8/FP32 of the measured "
          "quantity at the same N. `marginal` uses the slope between the two largest common N "
          "(bytes per added vector), i.e. with the fixed overhead removed.\n")
md.append("| System | N | metric | FP32 GB | SQ8 GB | actual reduction | theoretical |")
md.append("|---|---|---|---|---|---|---|")
P1 = [r for r in final if r["tag"] == "p1-32g" and not r["failure"]]
slopes = {}
for sysn in ("milvus", "qdrant", "vespa"):
    a = {r["N"]: r for r in P1 if r["system"] == sysn and r["representation"] == "FP32"}
    b = {r["N"]: r for r in P1 if r["system"] == sysn and r["representation"] == "SQ8"}
    common = sorted(set(a) & set(b))
    if not common:
        continue
    for n in common[-2:]:
        for m, lab in (("db_index_vector_gb", "DB index/vec"), ("process_rss_gb", "proc RSS"),
                       ("process_rss_anon_gb", "proc RSS anon"), ("pod_allprocs_rss_gb", "pod RSS"),
                       ("cgroup_gb", "cgroup"), ("peak_cgroup_gb", "peak cgroup")):
            x, y = a[n][m], b[n][m]
            if x and y:
                md.append(f"| {sysn} | {n:,} | {lab} | {x:.2f} | {y:.2f} | {100*(1-y/x):.1f} % | 75 % |")
    if len(common) >= 2:
        n1, n2 = common[-2], common[-1]
        for m, lab in (("process_rss_gb", "proc RSS"), ("pod_allprocs_rss_gb", "pod RSS"),
                       ("cgroup_gb", "cgroup"), ("peak_cgroup_gb", "peak cgroup"),
                       ("db_index_vector_gb", "DB index/vec")):
            try:
                sa = (a[n2][m] - a[n1][m]) * G / (n2 - n1)
                sb = (b[n2][m] - b[n1][m]) * G / (n2 - n1)
            except TypeError:
                continue
            if not sa:
                continue
            slopes[(sysn, m)] = (sa, sb, n1, n2)
            md.append(f"| {sysn} | marginal {n1:,}→{n2:,} | {lab} B/vector | {sa:.0f} | {sb:.0f} | "
                      f"{100*(1-sb/sa):.1f} % | 75 % (3072→768 B) |")

# ---- capacity at fixed budgets ------------------------------------------------------
md.append("\n## Vectors that fit in a fixed RAM budget\n")
md.append("**Measured**: last N that completed build+load+query without failure, and the first "
          "failing N with its failure type (step granularity: 8 GiB 0.5 M, 4 GiB 0.25 M, 16 GiB 1 M).\n")
md.append("| System | Repr | limit | last OK N | first failing N | failure | first thrashing N | last OK N w/o thrashing | SQ8/FP32 capacity (hard) | SQ8/FP32 capacity (no thrash) |")
md.append("|---|---|---|---|---|---|---|---|---|---|")
cap = {}
for tag in ("p3-4g", "p2-8g", "p4-16g"):
    for sysn in ("milvus", "qdrant", "vespa"):
        for mode in ("fp32", "sq8"):
            rs = [r for r in final if r["tag"] == tag and r["system"] == sysn and r["representation"] == mode.upper()]
            o = outcomes.get((tag, f"{sysn}-{mode}"))
            if not rs and not o:
                continue
            ok = [r["N"] for r in rs if not r["failure"] and r["qps"]]
            thr = [r["N"] for r in rs if r["pressure"]]
            ok_nothrash = [n for n in ok if not thr or n < min(thr)]
            bad = [r for r in rs if r["failure"]]
            fail_kind = bad[0]["failure"] if bad else ((o or {}).get("failure") or [None])[0]
            if not bad and o and o.get("failure"):
                bad = [{"N": None, "failure": o["failure"][0]}]
            cap[(tag, sysn, mode)] = (max(ok) if ok else 0, bad[0]["N"] if bad else None, fail_kind,
                                     o is not None and "ended" in o,
                                     max(ok_nothrash) if ok_nothrash else 0, min(thr) if thr else None)
        for mode in ("fp32", "sq8"):
            c = cap.get((tag, sysn, mode))
            if not c:
                continue
            ratio = ratio2 = ""
            if mode == "sq8" and (tag, sysn, "fp32") in cap:
                fc = cap[(tag, sysn, "fp32")]
                lb = " (lower bound: SQ8 never failed)" if c[1] is None else ""
                ratio = (f"{c[0]/fc[0]:.2f}×{lb}" if fc[0] else "FP32 fit 0") if fc[1] or c[1] \
                    else "n/a (neither hard-failed)"
                if fc[5] or c[5]:
                    lb2 = " (lower bound)" if c[5] is None else ""
                    ratio2 = f"{c[4]/fc[4]:.2f}×{lb2}" if fc[4] else ""
            status = "" if c[3] else " (running/incomplete)"
            md.append(f"| {sysn} | {mode.upper()} | {tag.split('-')[1]} | {c[0]:,} | "
                      f"{(f'{c[1]:,}' if c[1] else 'after last OK') if c[2] else 'none up to max step'} | "
                      f"{c[2] or '–'}{status} | {f'{c[5]:,}' if c[5] else '–'} | {c[4]:,} | {ratio} | {ratio2} |")

open(f"{ROOT}/results/SUMMARY.md", "w").write("\n".join(md) + "\n")
print("\n".join(md))
