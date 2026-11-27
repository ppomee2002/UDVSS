#!/usr/bin/env python3
"""Post-hoc recomputation of Milvus' DB-reported memory for runs whose live parse
used only the LAST traceID (a compaction hand-off after load() logs its own
prediction under a new traceID, so the live number could be one segment only).

For each step: load window = [build-row utc - load_seconds - 5s, build-row utc + 2s]
(build-row utc is written right after load completes; load_seconds from the
'settle info' line).  Sum maxSegmentSize(MB) over the loader prediction lines in
that window, from the saved engine_pod.log (tail 3000 lines -> early steps may be
missing; those are marked 'unavailable', never guessed).
Writes db_reported_fixed.jsonl next to rows.csv.
"""
import datetime as dt, glob, json, re, sys
ROOT = "/home/dev_path/experiments/memfootprint-sq8"

def ts_log(l):
    m = re.match(r"\[(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\.\d+)", l)
    return dt.datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S.%f").replace(
        tzinfo=dt.timezone.utc).timestamp() if m else None

def ts_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()

for d in sorted(glob.glob(f"{ROOT}/results/p[0-9]*/milvus-*/")):
    try:
        log = open(d + "engine_pod.log").read().splitlines()
    except FileNotFoundError:
        continue
    pred = [(ts_log(l), l) for l in log if "predict memory and disk usage while loading" in l]
    first_log_t = ts_log(next((l for l in log if ts_log(l)), "")) if log else None
    rows = [json.loads(l) for l in open(d + "rows.jsonl")]
    runlog = open(d + "run.log").read().splitlines()
    infos = [json.loads(l.split("settle info ", 1)[1]) for l in runlog if "settle info" in l]
    builds = [r for r in rows if r["phase"] == "build" and not r["failure"]]
    out = []
    for r, info in zip(builds, infos):
        t1 = ts_iso(r["utc"]); t0 = t1 - info["load_seconds"] - 5
        sel = [l for t, l in pred if t and t0 <= t <= t1 + 2]
        if first_log_t is None or first_log_t > t0:
            out.append({"N": r["N"], "m_load_pred_seg_mem_gb": None, "note": "log truncated (tail 3000)"})
            continue
        mb = sum(float(re.search(r"maxSegmentSize\(MB\)=([\d.]+)", l).group(1)) for l in sel)
        out.append({"N": r["N"], "m_load_pred_seg_mem_gb": round(mb / 1024, 4), "pred_lines": len(sel),
                    "bytes_per_row": round(mb * 2 ** 20 / r["N"], 1)})
    with open(d + "db_reported_fixed.jsonl", "w") as fh:
        for o in out:
            fh.write(json.dumps(o) + "\n")
    print(d.split("results/")[1], out)
