#!/usr/bin/env python3
"""Merge the Deep1B 100M results under a 48 GiB memory cap ("mem48") of Ceph / Milvus / Qdrant /
Vespa into CSVs + a Markdown table, next to the uncapped 100M baseline (make_100m_csv.py).

Writes  mem48_all.csv          every .row of the mem48 runs (incl. C-ladder tune rows)
        mem48_summary.csv      measured points per system x knob, with the uncapped baseline
        mem48_load_status.csv  one line per arm: can it load/run within 48 GiB, and how
        mem48_tables.md        the same as tables + caveats
"""
import csv, glob, os, statistics as st
import make_100m_csv as B   # column layouts + row readers (re-runs the 100M merge, harmless)

ROOT, OUT = B.ROOT, B.OUT

ARMS = {  # system -> (result dir, mode label)   [48 GiB main arms]
    "ceph":   ("ceph/results/d100m_m48noomap", "mem48 noomap + PG-log tuning"),
    "milvus": ("engines/results/milvus/deep1b100m_mem48_mmap", "mem48 mmap"),
    "qdrant": ("engines/results/qdrant/deep1b100m_mem48_default", "mem48 default"),
    "vespa":  ("engines/results/vespa/deep1b100m_mem48_paged", "mem48 paged"),
}
# extra arms (2026-10-17/18): (system, cap GiB, result dir, file glob, mode label, phase)
EXTRA = [
    ("ceph", 48, "ceph/results/d100m_m48noomap", "*_rrp1.row",
     "mem48 noomap + PG-log tuning, recall points (2026-10-17, during Vespa feed)", "recallpts"),
    ("vespa", 40, "engines/results/vespa/deep1b100m_mem40_paged", "*.row", "mem40 paged", None),
]

def norm(system, p):
    if system == "ceph":
        r = B.read_row(p, B.CEPH_COLS)
        return dict(knob="M", knob_value=int(r["M"]), concurrency=int(r["conc"]), rep=r["rep"],
                    qps=float(r["logical_qps"]), recall_at_10=float(r["recall_at_top_k"]),
                    failures=int(r["failures"]), latency_unit=f"batch of {r['batch']} queries",
                    lat_avg_ms=B.ms(r["logical_latency_us_avg"]), lat_p50_ms=B.ms(r["logical_latency_us_p50"]),
                    lat_p95_ms=B.ms(r["logical_latency_us_p95"]), lat_p99_ms=B.ms(r["logical_latency_us_p99"]),
                    queries_completed=int(r["completed"]), measure_seconds=float(r["seconds"]),
                    candidates_per_query=r["avg_total_matching_records_per_query"],
                    server_cpu_cores=r["osd_cores"], client_cpu_cores=r["client_cores"],
                    start_utc=r["start_utc"], end_utc=r["end_utc"], status=r["status"],
                    config="1 crimson OSD (cpuset 0-7), PG16384, balanced pivot, 48G cgroup; "
                           "CRIMSON_VECTOR_NO_OMAP_IDENTITY=1, osd_pg_log_dups_tracked=100, osd_min_pg_log_entries=50")
    if system == "milvus":
        r = B.read_row(p, B.MILVUS_COLS)
        return dict(knob="ef", knob_value=int(r["ef"]), concurrency=int(r["concurrency"]), rep=r["repeat"],
                    qps=float(r["qps_vectors"]), recall_at_10=float(r["recall_at_10"]),
                    failures=int(r["failures"]), latency_unit=f"batch of {r['nq']} queries",
                    lat_avg_ms=B.ms(r["req_latency_us_avg"]), lat_p50_ms=B.ms(r["req_latency_us_p50"]),
                    lat_p95_ms=B.ms(r["req_latency_us_p95"]), lat_p99_ms=B.ms(r["req_latency_us_p99"]),
                    queries_completed=int(r["completed_vectors"]), measure_seconds=float(r["seconds"]),
                    candidates_per_query="", server_cpu_cores=r["milvus_cpu_cores"],
                    client_cpu_cores=r["client_cpu_cores"], start_utc=r["start_utc"], end_utc=r["end_utc"],
                    status=r["status"],
                    config=f"milvus {r['milvus_version']} HNSW M{r['index_M']} efc{r['index_efConstruction']}, "
                           "cpuset 0-7, 48G cap, queryNode mmap vectorField+vectorIndex")
    r = B.read_row(p, B.ENG_COLS)
    unit = int(r["unit_vectors"])
    extra = {"qdrant": "48G cap, original config (on_disk=False)", "vespa": "48G cap, attribute: paged"}[system]
    return dict(knob="ef", knob_value=int(r["ef"]), concurrency=int(r["concurrency"]), rep=r["repeat"],
                qps=float(r["qps_vectors"]), recall_at_10=float(r["recall_at_10"]),
                failures=int(r["failures"]), latency_unit="1 query" if unit == 1 else f"batch of {unit} queries",
                lat_avg_ms=B.ms(r["unit_latency_us_avg"]), lat_p50_ms=B.ms(r["unit_latency_us_p50"]),
                lat_p95_ms=B.ms(r["unit_latency_us_p95"]), lat_p99_ms=B.ms(r["unit_latency_us_p99"]),
                queries_completed=int(r["completed_vectors"]), measure_seconds=float(r["seconds"]),
                candidates_per_query="", server_cpu_cores=r["engine_cpu_cores"],
                client_cpu_cores=r["client_cpu_cores"], start_utc=r["start_utc"], end_utc=r["end_utc"],
                status=r["status"],
                config=f"{system} {r['engine_version']} HNSW M{r['index_M']} efc{r['index_efConstruction']}, "
                       f"cpuset {'12-19' if system == 'vespa' else '0-7'}, {extra}")

rows = []
SRC = [(sy, 48, d, "*.row", mode, None) for sy, (d, mode) in ARMS.items()] + EXTRA
for system, cap, d, pat, mode, phase in SRC:
    for p in sorted(glob.glob(f"{ROOT}/{d}/{pat}")):
        if phase is None and p.endswith("_rrp1.row"): continue   # recall points are their own arm
        r = dict(system=system, cap_gb=cap, dataset="deep1b100m", N=100_000_000, mode=mode, **norm(system, p))
        r["phase"] = phase or ("tune" if r["rep"] in ("tune", "rtune") else "sweep")
        r["source_file"] = os.path.relpath(p, ROOT)
        if cap == 40: r["config"] = r["config"].replace("48G cap", "40G cap")
        rows.append(r)
order = {"ceph": 0, "milvus": 1, "qdrant": 2, "vespa": 3}
PH = {"sweep": 0, "recallpts": 1, "tune": 2}
rows.sort(key=lambda r: (order[r["system"]], -r["cap_gb"], PH[r["phase"]], r["knob_value"], r["concurrency"], r["rep"]))
AF = ["system", "cap_gb", "dataset", "N", "mode", "knob", "knob_value", "concurrency", "rep", "phase", "qps",
      "recall_at_10", "failures", "latency_unit", "lat_avg_ms", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms",
      "queries_completed", "measure_seconds", "candidates_per_query", "server_cpu_cores",
      "client_cpu_cores", "start_utc", "end_utc", "status", "config", "source_file"]
with open(f"{OUT}/mem48_all.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, AF); w.writeheader(); w.writerows(rows)

# uncapped 100M baseline (scale_deep_100m_summary.csv, 3 reps each)
base = {(r["system"], int(r["knob_value"])): r
        for r in csv.DictReader(open(f"{OUT}/scale_deep_100m_summary.csv"))}

NOTES = {
    ("qdrant", 32): "cliff: page-cache thrash starts between ef16 and ef32",
    ("vespa", 512): "collapse: page-cache thrash from ef512 (~1 TB disk reads)",
}
NOTES40 = {("vespa", 256): "collapse at 40G: page-cache thrash from ef256 (48G: from ef512)"}
SKIPPED = {("milvus", 512): "not measured (skipped by user, deadline)",
           ("milvus", 1024): "not measured (skipped by user, deadline)"}
KNOBS = {"ceph": [8, 16, 32, 64, 96, 128], "milvus": [10, 16, 32, 64, 128, 256, 512, 1024],
         "qdrant": [10, 16, 32, 64, 128, 256, 512, 1024], "vespa": [10, 16, 32, 64, 128, 256, 512, 1024]}
groups = {}
for r in rows:
    if r["phase"] != "tune":
        groups.setdefault((r["system"], r["cap_gb"], r["phase"], r["knob_value"]), []).append(r)
SUMARMS = [(sy, 48, "sweep", ARMS[sy][1], KNOBS[sy]) for sy in order] + [
    ("ceph", 48, "recallpts", EXTRA[0][4], [16, 20, 24, 32, 36, 40]),
    ("vespa", 40, "sweep", "mem40 paged", KNOBS["vespa"])]
SUMARMS.sort(key=lambda a: (order[a[0]], -a[1], a[2] != "sweep"))

def mean(xs):
    xs = [float(x) for x in xs if x not in ("", "NA")]
    return round(st.mean(xs), 4) if xs else ""

SF = ["system", "cap_gb", "phase", "mode", "knob", "knob_value", "concurrency", "reps", "qps_mean", "qps_min", "qps_max",
      "recall_at_10", "failures", "lat_p50_ms", "lat_p99_ms", "latency_unit", "server_cpu_cores",
      "uncapped_concurrency", "uncapped_reps", "uncapped_qps", "uncapped_recall_at_10",
      "qps_ratio_vs_uncapped", "slowdown_x", "status", "note"]
summary = []
for system, cap, phase, mode, knobs in SUMARMS:
    for k in knobs:
        b = base.get((system, k))
        s = dict(system=system, cap_gb=cap, phase=phase, mode=mode, knob="M" if system == "ceph" else "ef", knob_value=k,
                 uncapped_concurrency=b["concurrency"] if b else "", uncapped_reps=b["reps"] if b else "",
                 uncapped_qps=b["qps_mean"] if b else "", uncapped_recall_at_10=b["recall_at_10_mean"] if b else "",
                 note=NOTES.get((system, k), "") if cap == 48 else NOTES40.get((system, k), ""))
        if phase == "recallpts" and k in (16, 32): s["note"] = "anchor: re-measured; ~11-12% below the 2026-10-16 sweep (interference)"
        g = groups.get((system, cap, phase, k))
        if not g:
            s.update(status="NOT_MEASURED", note=SKIPPED.get((system, k), "not measured"))
            summary.append(s); continue
        q = [x["qps"] for x in g]
        s.update(concurrency=g[0]["concurrency"], reps=len(g), qps_mean=round(st.mean(q), 1),
                 qps_min=round(min(q), 1), qps_max=round(max(q), 1),
                 recall_at_10=mean(x["recall_at_10"] for x in g), failures=sum(x["failures"] for x in g),
                 lat_p50_ms=mean(x["lat_p50_ms"] for x in g), lat_p99_ms=mean(x["lat_p99_ms"] for x in g),
                 latency_unit=g[0]["latency_unit"], server_cpu_cores=mean(x["server_cpu_cores"] for x in g),
                 status="/".join(sorted({x["status"] for x in g})))
        if b:
            ratio = st.mean(q) / float(b["qps_mean"])
            s.update(qps_ratio_vs_uncapped=round(ratio, 3), slowdown_x=round(1 / ratio, 2))
        summary.append(s)
with open(f"{OUT}/mem48_summary.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, SF); w.writeheader(); w.writerows(summary)

# ---- load / run status per arm (from chain.log, sampler TSVs, engine/OSD logs) ----
LF = ["system", "cap_gb", "arm", "mode", "settings", "result", "load_phases_s", "cgroup_peak_gb", "max_anon_gb",
      "oom_kill", "evidence"]
status = [
    dict(system="ceph", arm="d100m_m48def", mode="Default",
         settings="original 100M config + 48G cgroup; crimson_memory unset (seastar auto ~44.6 GiB = 48G - 7%, 8 shards x ~5.57G)",
         result="Cannot load within 48 GiB: crimson-osd std::bad_alloc on shard 0 at ~81% of load (~69M writes), 2026-10-16T05:49:46Z",
         load_phases_s="~31800 s (create + 81% of writes) until crash", cgroup_peak_gb=48.01, max_anon_gb=44.13, oom_kill=0,
         evidence="mem48/ceph_default_evidence/, mem48/ceph_crash_20261016T055027Z.log, mem48/ceph_default_mem.tsv; root cause: PG-log dup entries (16384 PGs x osd_pg_log_dups_tracked 3000)"),
    dict(system="ceph", arm="d100m_m48noomap", mode="Memory-constrained (Ceph settings)",
         settings="48G cgroup, crimson_memory unset; CRIMSON_VECTOR_NO_OMAP_IDENTITY=1 (dev flag); osd_pg_log_dups_tracked 3000->100 + osd_min_pg_log_entries 250->50 applied at runtime at ~44M writes; LOAD_CONC 256",
         result="Loads and runs within 48 GiB; M8..M128 all measured, 0 failures",
         load_phases_s="writes 21595 s", cgroup_peak_gb=44.04, max_anon_gb=31.82, oom_kill=0,
         evidence="mem48/chain.log, mem48/ceph_noomap_mem.tsv (seastar shard alloc max ~3.0G of 5.57G), ceph/logs/d100m_m48noomap_create.log"),
    dict(system="milvus", arm="mem48_default", mode="Default",
         settings="milvus 2.6.23 HNSW M30 efc360, original config + 48G cap (vectorField mmap=true is 2.6 default, vectorIndex mmap=false)",
         result="Cannot load within 48 GiB: load_collection refused by Milvus (segment request resource failed[resourceType=Memory])",
         load_phases_s="insert 6643 + index 8326; load refused", cgroup_peak_gb=43.52, max_anon_gb=42.95, oom_kill=0,
         evidence="engines/logs/milvus_deep1b100m_mem48_default.log, mem48/milvus_mem48_default.tsv"),
    dict(system="milvus", arm="mem48_mmap", mode="Memory-constrained (official mmap)",
         settings="QUERYNODE_MMAP_VECTORFIELD=true + QUERYNODE_MMAP_VECTORINDEX=true (checked in querynode log); same index/params",
         result="Loads and runs within 48 GiB; ef10..ef256 measured, ef512/1024 skipped",
         load_phases_s="insert 6604 + index 8339 + load 156 s", cgroup_peak_gb=48.00, max_anon_gb=7.62, oom_kill=0,
         evidence="engines/logs/milvus_deep1b100m_mem48_mmap.log, mem48/milvus_mem48_mmap.tsv (9.2 TB cumulative disk reads)"),
    dict(system="qdrant", arm="mem48_default", mode="Default",
         settings="qdrant 1.19.1 HNSW m30 efc360, on_disk=False (original config) + 48G cap",
         result="Loads and runs within 48 GiB (vectors are file-backed and page out instead of OOM); memory-constrained mode not needed",
         load_phases_s="feed 2065 + index 18813 s", cgroup_peak_gb=48.00, max_anon_gb=16.53, oom_kill=0,
         evidence="engines/logs/qdrant_deep1b100m_mem48_default.log, mem48/qdrant_mem48_default.tsv (3.3 TB cumulative disk reads)"),
    dict(system="vespa", arm="mem48_default", mode="Default",
         settings="vespa HNSW M30 efc360, non-paged attribute (original config) + 48G cap",
         result="Cannot load within 48 GiB: vespa-proton-bin OOM-killed 3x within 2 min (anon-rss ~45.3 GiB)",
         load_phases_s="index restart from flushed 100M index", cgroup_peak_gb=48.0, max_anon_gb=45.35, oom_kill=3,
         evidence="mem48/oom_kernel_a3.log, mem48/vespa_mem48_cgroup.tsv phase (a)"),
    dict(system="vespa", arm="mem48_paged", mode="Memory-constrained (official paged attribute)",
         settings="attribute: paged on the same index (no re-feed)",
         result="Loads and runs within 48 GiB; ef10..ef1024 measured (3 reps)",
         load_phases_s="deploy + restart only", cgroup_peak_gb=48.00, max_anon_gb=20.61, oom_kill=0,
         evidence="engines/logs/vespa_deep1b100m_mem48_paged.log, mem48/vespa_mem48_cgroup.tsv phase (b)"),
]
for x in status: x["cap_gb"] = 48
status += [
    dict(system="vespa", cap_gb=40, arm="mem40_default", mode="Default",
         settings="non-paged attribute (original config) + 40G cap", result="Cannot load within 40 GiB (not re-run: already OOM-killed 3x at 48G with anon ~45.3 GiB > 40G)",
         load_phases_s="", cgroup_peak_gb="", max_anon_gb="", oom_kill="", evidence="inferred from vespa mem48_default"),
    dict(system="vespa", cap_gb=40, arm="mem40_paged", mode="Memory-constrained (official paged attribute)",
         settings="attribute: paged; index re-fed from scratch uncapped with the paged schema (VB_DISK_LIMIT 0.97 for the feed only), clean stop, restart under 40G",
         result="Loads and runs within 40 GiB; serving 4 min after restart; ef10..ef1024 measured (3 reps)",
         load_phases_s="feed ~18.9 h (incl. 52 min pause + ~2 h page-cache stall) + restart 4 min", cgroup_peak_gb=40.00, max_anon_gb=23.45, oom_kill=0,
         evidence="scale-deep/mem40/driver.log, run_b.out, vespa_mem40_cgroup.tsv (disk reads 61 -> 1913 GB during ef256..1024)"),
]
with open(f"{OUT}/mem48_load_status.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, LF); w.writeheader(); w.writerows(status)

# ---- QPS at recall@10 = 0.90 / 0.95 (log-QPS linear in recall between bracketing points) ----
import math
def interp(pts, t):
    pts = sorted(pts, key=lambda x: x[1])
    if pts[0][1] >= t: return None, f"already {pts[0][1]:.4f} at the lowest {pts[0][0]} (QPS {pts[0][2]})", pts[0], pts[0]
    for a, b in zip(pts, pts[1:]):
        if a[1] < t <= b[1]:
            fr = (t - a[1]) / (b[1] - a[1])
            return round(math.exp(math.log(a[2]) + fr * (math.log(b[2]) - math.log(a[2]))), 1), "", a, b
    return None, "not reached", pts[-1], pts[-1]
TF = ["system", "cap_gb", "arm", "target_recall", "qps_interp", "lo_knob", "lo_recall", "lo_qps", "hi_knob", "hi_recall", "hi_qps",
      "uncapped_qps_interp", "note"]
targets = []
def arm_pts(system, cap, phase):
    return [(x["knob_value"], float(x["recall_at_10"]), float(x["qps_mean"])) for x in summary
            if x["system"] == system and x["cap_gb"] == cap and x["phase"] == phase and x.get("qps_mean") not in ("", None)]
ceph_fix = st.mean(float(base[("ceph", k)]["qps_mean"]) and
                   [float(x["qps_mean"]) for x in summary if x["system"] == "ceph" and x["phase"] == "sweep" and x["knob_value"] == k][0] /
                   [float(x["qps_mean"]) for x in summary if x["system"] == "ceph" and x["phase"] == "recallpts" and x["knob_value"] == k][0]
                   for k in (16, 32))
for system, cap, phase, mode, _ in SUMARMS:
    pts = arm_pts(system, cap, phase)
    up = [(k, float(base[(system, k)]["recall_at_10_mean"]), float(base[(system, k)]["qps_mean"])) for k in KNOBS[system] if (system, k) in base]
    for t in (0.90, 0.95):
        q, why, a, b = interp(pts, t); qu = interp(up, t)[0]
        note = why
        if system == "qdrant" and cap == 48 and t == 0.95: note = "cliff between ef16 and ef32: interpolation unreliable; measured ef32 = 112 QPS @0.968"
        if phase == "recallpts" and q: note = f"measured during the Vespa feed; anchor-corrected x{ceph_fix:.3f} = {q * ceph_fix:.0f}"
        if system == "ceph" and phase == "sweep": note = (note + "; " if note else "") + "coarse M grid (see recallpts arm for M20/24/36/40)"
        targets.append(dict(system=system, cap_gb=cap, arm=mode, target_recall=t, qps_interp=q if q else "", lo_knob=a[0], lo_recall=a[1], lo_qps=a[2],
                            hi_knob=b[0], hi_recall=b[1], hi_qps=b[2], uncapped_qps_interp=qu or "", note=note))
with open(f"{OUT}/mem48_recall_targets.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, TF); w.writeheader(); w.writerows(targets)

# ---- Markdown ----
def f(x, nd=1):
    return "" if x in ("", None) else (f"{float(x):.{nd}f}")
md = ["# Deep1B 100M under a memory cap: 48 GiB (all systems) + 40 GiB (Vespa)", "",
      "Files: mem48_all.csv (every row), mem48_summary.csv (per arm x knob, with uncapped baseline), mem48_recall_targets.csv "
      "(QPS at recall 0.90/0.95), mem48_load_status.csv (load/run per arm), mem40_vespa_summary.csv; 4 GiB Ceph capacity: cap4g_*.csv / cap4g_tables.md.", "",
      "## 1. Can it load/run within 48 GiB?", "",
      "| System | Default (original config + 48G) | Memory-constrained mode used | Result | Load time (mode used) | max anon GiB (mode used) | oom_kill (mode used) |",
      "|---|---|---|---|---|---|---|"]
for system in order:
    d = [s for s in status if s["system"] == system and s["mode"] == "Default" and s["cap_gb"] == 48][0]
    m = [s for s in status if s["system"] == system and s["mode"] != "Default" and s["cap_gb"] == 48]
    if m:
        m = m[0]
        md.append(f"| {system} | **Cannot load** ({d['result'].split(': ', 1)[1]}) | {m['mode'].split('(')[1].rstrip(')')} | {m['result']} | {m['load_phases_s']} | {m['max_anon_gb']} | {m['oom_kill']} |")
    else:
        md.append(f"| {system} | **Loads** | not needed | {d['result']} | {d['load_phases_s']} | {d['max_anon_gb']} | {d['oom_kill']} |")
v40 = [s for s in status if s["cap_gb"] == 40]
md += ["", "40 GiB (Vespa only, 2026-10-17): default **Cannot load** (inferred: anon ~45.3 GiB at 48G); "
       f"paged: {v40[1]['result']}; max anon {v40[1]['max_anon_gb']} GiB, oom_kill {v40[1]['oom_kill']}.",
       "", "## 2. QPS / recall@10 under the cap vs uncapped", "",
       "slowdown = uncapped QPS / capped QPS. Concurrency: capped C (uncapped C).", ""]
for system, cap, phase, mode, _ in SUMARMS:
    md += [f"### {system} {cap}G ({mode})", "",
           f"| {'M' if system == 'ceph' else 'ef'} | C | reps | QPS ({cap}G) | recall@10 | p50 ms | p99 ms | uncapped QPS (C) | uncapped recall | slowdown | note |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        if (s["system"], s["cap_gb"], s["phase"]) != (system, cap, phase): continue
        if s["status"] == "NOT_MEASURED":
            md.append(f"| {s['knob_value']} | | | — | | | | {f(s['uncapped_qps'])} ({s['uncapped_concurrency']}) | {f(s['uncapped_recall_at_10'],4)} | | {s['note']} |"); continue
        rng = f" ({s['qps_min']}–{s['qps_max']})" if s["reps"] > 1 else ""
        md.append(f"| {s['knob_value']} | {s['concurrency']} | {s['reps']} | {s['qps_mean']}{rng} | {f(s['recall_at_10'],4)} | "
                  f"{f(s['lat_p50_ms'])} | {f(s['lat_p99_ms'])} | {(f(s['uncapped_qps']) + ' (' + str(s['uncapped_concurrency']) + ')') if s['uncapped_qps'] else '—'} | "
                  f"{f(s['uncapped_recall_at_10'],4) or '—'} | {(str(s['slowdown_x']) + '×') if s.get('slowdown_x') else '—'} | {s['note']} |")
    md.append("")
md += ["## 3. QPS at recall@10 = 0.90 / 0.95 (interpolated, log-QPS linear in recall)", "",
       "| system | cap | arm | target | QPS | bracket (knob recall QPS) | uncapped QPS | note |", "|---|---|---|---|---|---|---|---|"]
for t in targets:
    md.append(f"| {t['system']} | {t['cap_gb']}G | {t['arm']} | {t['target_recall']} | {t['qps_interp'] or '—'} | "
              f"{t['lo_knob']} {t['lo_recall']:.4f} {t['lo_qps']} → {t['hi_knob']} {t['hi_recall']:.4f} {t['hi_qps']} | {t['uncapped_qps_interp'] or '—'} | {t['note']} |")
md += ["", "## 4. Concurrency selection (C ladder at ef64 / fixed C)", "",
       "| system | C | QPS | recall | failures | status |", "|---|---|---|---|---|---|"]
for r in rows:
    if r["phase"] == "tune":
        md.append(f"| {r['system']} {r['cap_gb']}G | {r['concurrency']} | {round(r['qps'],1)} | {r['recall_at_10']:.4f} | {r['failures']} | {r['status']} |")
md += ["| ceph | 20 | fixed (no ladder: memory guard) | | | |", "",
       "Ladder rows are separate tune runs (Qdrant C=20 tune 62.2 vs sweep ef64 52.8: run-to-run variance under paging).", "",
       "## 5. Caveats (report with the numbers)", "",
       "- Concurrency differs: Vespa C=160 (ladder), Qdrant/Milvus/Ceph C=20; uncapped baseline: Ceph C=80, Vespa C=80, Milvus/Qdrant C=20.",
       "- Reps: Vespa 3 reps per ef; Qdrant, Milvus and Ceph 1 rep per point (deadline; C ladder stopped early).",
       "- Measurement: 120 s time-bound per point, serial warm-up, in-flight requests drained; 1024 queries per request (Vespa: single queries).",
       "- Ceph memory-constrained arm: CRIMSON_VECTOR_NO_OMAP_IDENTITY=1 is a dev flag (no per-vector OMAP identity key; results return entry_id, user key not restored). "
       "PG-log settings (osd_pg_log_dups_tracked 100, osd_min_pg_log_entries 50) are official options but were applied at runtime at ~44M writes; the first ~44% of the load ran with defaults. Neither affects the query path's work.",
       "- Ceph default failure root cause: PG-log dup entries (16384 PGs × 3000 = up to 49M in-memory entries), not cache; the OMAP identity key explains only ~21-23% (10M A/B).",
       "- Milvus mmap: ef512/1024 not measured. Milvus client has no 600 s timeout; Qdrant server-side search timeout is 600 s (Qdrant C=40 had 11 timeouts, so C=20).",
       "- Qdrant default fits only because its in-RAM vectors are file-backed mmap that the kernel can page out (cgroup anon ≤16.5G, rest page cache).",
       "- Vespa paged: ef512 QPS varies a lot between reps (184–255) because of page-cache thrash.",
       "- cgroup peak includes page cache, so 48.00 means the cap was reached by cache, not necessarily by anon memory.",
       "- Ceph recall points (M16/20/24/32/36/40, 2026-10-17 09:27-09:46Z) ran on the same live cluster while Vespa fed on other cores; "
       f"anchors M16/M32 are ~11-12% slower than the 2026-10-16 sweep (recall identical) -> anchor-corrected factor x{ceph_fix:.3f}.",
       "- Vespa 40G: index re-fed from scratch with the paged schema (48G: paged deploy of the uncapped-run index); same params, recall within 0.003. "
       "Feed paused 52 min (FP32 4G Ceph run) and stalled ~2 h by host page-cache pressure (fixed by reclaiming dev1 cache); neither touches the measurement. "
       "dev1 page cache was auto-reclaimed (>5G -> 1G) during the 40G run so Vespa got its full 40G.",
       "- Vespa 40G vs 48G: identical up to ef128 (≤0.5%); collapse moves from ef512 (48G) to ef256 (40G).", ""]
open(f"{OUT}/mem48_tables.md", "w").write("\n".join(md))
print(len(rows), "rows;", len(summary), "summary rows;", len(status), "arms")
