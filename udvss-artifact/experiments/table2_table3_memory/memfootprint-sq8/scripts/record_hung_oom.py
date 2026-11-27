#!/usr/bin/env python3
"""Record an OOMKilled step for runs whose feeder hung after the pod died
(p3-4g vespa, fixed in engines.py afterwards).  The row is built ONLY from raw
sampler lines between the step's '# MARK build N=' and the last sample before
the stream ended; pod status (OOMKilled, exit 137) is from kubectl."""
import json, sys, time
sys.path.insert(0, "/home/dev_path/experiments/memfootprint-sq8/scripts")
import common as C
d, n, prev = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
system, mode = d.rstrip("/").split("/")[-1].split("-")
lines = open(d + "/samples.log").read().splitlines()
start = max(i for i, l in enumerate(lines) if l.startswith(f"# MARK") and f"build N={n}" in l)
win = []
for l in lines[start + 1:]:
    if l.startswith("# sampler stream ended"):
        break
    s = C.parse_sample(l)
    if "cg_cur" in s:
        win.append(s)
procs = {"vespa": {"vespa-proton-bi"}, "milvus": {"milvus"}, "qdrant": {"qdrant"}}[system]
summ = C.summarize(win, procs)
G = 1024 ** 3
row = dict(system=system, mode=mode, mem_limit="4Gi", tag="p3-4g", phase="build", N=n, dim=C.DIM,
           raw_vec_gb_fp32=round(n * C.DIM * 4 / G, 4),
           raw_vec_gb_repr=round(n * C.DIM * (4 if mode == "fp32" else 1) / G, 4),
           failure="OOMKilled",
           failure_msg=f"pod OOMKilled (exit 137) during insert {prev}->{n}; last sample t_ms={win[-1]['t_ms']}; "
                       "row recorded post hoc by record_hung_oom.py",
           utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(win[-1]["t_ms"] / 1000))) | summ
hdr = open(d + "/rows.csv").readline().strip().split(",")
with open(d + "/rows.csv", "a") as fh:
    fh.write(",".join(str(row.get(k, "")).replace(",", ";") for k in hdr) + "\n")
with open(d + "/rows.jsonl", "a") as fh:
    fh.write(json.dumps(row) + "\n")
o = json.load(open(d + "/outcome.json"))
o["failure"] = ["OOMKilled", row["failure_msg"]]; o["last_N_ok"] = prev
o["note"] = "feeder hung on the dead pod for ~2.5 h (harness bug, fixed); outcome set post hoc"
json.dump(o, open(d + "/outcome.json", "w"), indent=1)
print(json.dumps({k: row[k] for k in ("system", "mode", "N", "peak_cg_cur_gb", "peak_rss_gb", "oom_kill", "utc")}))
