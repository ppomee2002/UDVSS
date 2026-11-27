#!/usr/bin/env python3
"""Ceph 4 GiB capacity sweep (memfootprint-sq8, reference config), SQ8 (int8) vs FP32, three configs:
OMAP on (prof12), OMAP off (noomap [+ noomap_ext for int8 6M/8M]), OMAP off + PG-log tuning (noomap_pglog).
Writes cap4g_all.csv (every row incl. load/stable phases), cap4g_summary.csv (one line per config x repr x N),
cap4g_max_n.csv (largest N that loads AND queries), cap4g_tables.md."""
import csv, json, os
R = "/home/dev_path/experiments/memfootprint-sq8/ceph/results"
OUT = os.path.dirname(os.path.abspath(__file__))
CONFIGS = [  # (config label, [result dirs])
    ("OMAP on", ["prof12"]),
    ("OMAP off", ["noomap", "noomap_ext"]),
    ("OMAP off + PG log", ["noomap_pglog"]),
]
DATES = {"prof12": "2026-10-03", "noomap": "2026-10-03", "noomap_ext": "2026-10-03", "noomap_pglog": "2026-10-16/17"}
SETTINGS = ("1 crimson OSD, smp 2, crimson_memory 4G (seastar 2G/shard), RANDOM_BLOCK_SSD + cbjournal 1288486912, "
            "cachepin 524288000/shard, PG 1024, pivot routing, M=16, 200 queries, conc 40 x batch 32, no OSD restart between N; "
            "cohere1m 768-d, N > 1M augmented (cohere[i % 1M] + N(0, 0.05^2), renormalised)")
allrows, summ = [], []
for cfg, dirs in CONFIGS:
    for repr_ in ("int8", "fp32"):
        byN = {}
        for d in dirs:
            p = f"{R}/{d}/{repr_}/rows.csv"
            if not os.path.exists(p): continue
            for r in csv.DictReader(open(p)):
                r = dict(config=cfg, repr="SQ8 (int8)" if repr_ == "int8" else "FP32", run_dir=d, run_date=DATES[d], **r)
                allrows.append(r)
                byN.setdefault(int(r["N"]), {})[r["phase"]] = r
        for n in sorted(byN):
            ph = byN[n]; ld, q = ph.get("load", {}), ph.get("query", {})
            fail = (q.get("failure") or ld.get("failure") or "")
            res = "ok" if q.get("qps") and not fail else (fail or "not run")
            summ.append(dict(config=cfg, repr=ld.get("repr") or q.get("repr"), N=n, load_s=ld.get("load_s", ""),
                             after_load_seastar_alloc_gb=ld.get("seastar_alloc_gb", ""), after_load_rss_gb=ld.get("rss_gb", ""),
                             query_rss_gb=q.get("rss_gb", ""), query_seastar_alloc_gb=q.get("seastar_alloc_gb", "") if res == "ok" else "",
                             cache_hit_ratio=q.get("cache_hit_ratio", ""), qps=q.get("qps", ""), lat_p50_ms=q.get("lat_p50_ms", ""),
                             lat_p99_ms=q.get("lat_p99_ms", ""), recall10=q.get("recall10", ""), result=res,
                             run_dir=(ld or q).get("run_dir"), run_date=(ld or q).get("run_date")))
keys = []
for r in allrows:
    for k in r:
        if k not in keys: keys.append(k)
with open(f"{OUT}/cap4g_all.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, keys); w.writeheader(); w.writerows(allrows)
with open(f"{OUT}/cap4g_summary.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, list(summ[0])); w.writeheader(); w.writerows(summ)
mx = []
for cfg, _ in CONFIGS:
    for rp in ("SQ8 (int8)", "FP32"):
        s = [x for x in summ if x["config"] == cfg and x["repr"] == rp]
        ok = [x for x in s if x["result"] == "ok"]; bad = [x for x in s if x["result"] != "ok"]
        mx.append(dict(config=cfg, repr=rp, max_n_ok=ok[-1]["N"] if ok else 0, qps_at_max=ok[-1]["qps"] if ok else "",
                       recall_at_max=ok[-1]["recall10"] if ok else "", first_fail_n=bad[0]["N"] if bad else "",
                       first_fail=bad[0]["result"] if bad else "", rss_at_fail_gb=bad[0]["query_rss_gb"] if bad else ""))
with open(f"{OUT}/cap4g_max_n.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, list(mx[0])); w.writeheader(); w.writerows(mx)

def M(n): return f"{n/1e6:g}M"
def f(x, nd=2): return "" if x in ("", None) else f"{float(x):.{nd}f}"
md = ["# Ceph capacity under 4 GiB: SQ8 vs FP32 (memfootprint-sq8, reference config)", "", "Settings: " + SETTINGS + ".",
      "OMAP off = CRIMSON_VECTOR_NO_OMAP_IDENTITY=1 (dev flag). PG log = osd_pg_log_dups_tracked=100 + osd_min_pg_log_entries=50 (in ceph.conf from creation).",
      "Each sweep stops at the first failing N.", "", "## 1. Largest N that loads and answers queries", "",
      "| config | SQ8 max N | SQ8 QPS / recall at max | FP32 max N | FP32 QPS / recall at max | first failure SQ8 / FP32 |", "|---|---|---|---|---|---|"]
for cfg, _ in CONFIGS:
    a, b = [x for x in mx if x["config"] == cfg]
    md.append(f"| {cfg} | **{M(a['max_n_ok'])}** | {f(a['qps_at_max'],1)} / {f(a['recall_at_max'],3)} | **{M(b['max_n_ok'])}** | "
              f"{f(b['qps_at_max'],1)} / {f(b['recall_at_max'],3)} | {M(a['first_fail_n'])} {a['first_fail']} (RSS {f(a['rss_at_fail_gb'])}) / "
              f"{M(b['first_fail_n'])} {b['first_fail']} (RSS {f(b['rss_at_fail_gb'])}) |")
md += ["", "## 2. Per N", ""]
for cfg, _ in CONFIGS:
    for rp in ("SQ8 (int8)", "FP32"):
        s = [x for x in summ if x["config"] == cfg and x["repr"] == rp]
        md += [f"### {cfg} — {rp} ({s[0]['run_date']})", "",
               "| N | load s | after-load seastar alloc GB | query OSD RSS GB (of 4) | cache hit | QPS | p99 ms | recall@10 | result |",
               "|---|---|---|---|---|---|---|---|---|"]
        for x in s:
            md.append(f"| {M(x['N'])} | {f(x['load_s'],0)} | {f(x['after_load_seastar_alloc_gb'])} | {f(x['query_rss_gb'])}"
                      f"{' at failure' if x['result'] != 'ok' and x['query_rss_gb'] else ''} | {f(x['cache_hit_ratio'],3)} | {f(x['qps'],1)} | "
                      f"{f(x['lat_p99_ms'],0)} | {f(x['recall10'],3)} | {'ok' if x['result'] == 'ok' else '**' + x['result'] + '**'} |")
        md.append("")
md += ["## 3. Notes", "",
       "- Failures are query-phase: std::bad_alloc on seastar shard 0 (OSD hangs, or dies in older runs) at OSD RSS ~3.5-3.8 GB of 4; load itself succeeds at the failing N.",
       "- OMAP off: int8 4M from noomap, 6M/8M from noomap_ext (same config, separate cluster, 2026-10-03).",
       "- N > 1M uses augmented near-duplicate data: fine for memory capacity, not representative for QPS/recall.",
       "- FP32 OMAP off + PG log ran 2026-10-17 13:28-14:17Z with the Vespa feed paused (no CPU overlap); SQ8 ran 2026-10-16 with the Vespa index deleted.", ""]
open(f"{OUT}/cap4g_tables.md", "w").write("\n".join(md))
print(len(allrows), "rows;", len(summ), "summary;"); print(open(f"{OUT}/cap4g_max_n.csv").read())
