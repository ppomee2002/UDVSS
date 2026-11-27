#!/bin/bash
# Re-measure the FP32 VEC runs after the reply-vector copy fix (1e83d515a3f):
# the old VEC latencies included copying every scanned candidate's vector.
# Traffic is unaffected; ID runs are kept. Waits for the SQ8 pipeline first.
E=/home/dev_path/experiments/ceph-s-qn-scaling
until grep -q "sq8 all done" $E/results_sq8/logs/30_sq8_all.log; do sleep 60; done
cd $E/results && python3 - <<'PY'
import pandas as pd, os, shutil
d = pd.read_csv("runs.csv")
bad = d[d["mode"] == "VEC"]
if len(bad):
    bad.to_csv("runs_invalid_vec_copyall.csv", index=False)
    d[d["mode"] != "VEC"].to_csv("runs.csv", index=False)
    os.makedirs("runs_invalid", exist_ok=True)
    for l in bad.label:
        for f in os.listdir("runs"):
            if f.startswith(l + "-"):
                shutil.move(os.path.join("runs", f), "runs_invalid")
print(len(bad), "FP32 VEC rows invalidated")
PY
# FP32 cluster from scratch on the default (FP32) bundle
source $E/scripts/env.sh
r109 "pkill -9 -f '[b]undle[-a-z0-9]*/bin/crimson-osd -i' || true"
kill $(exp_pids) 2>/dev/null; sleep 3
$E/scripts/00_mon.sh && $E/scripts/netctr.sh setup && $E/scripts/01_osds.sh 1
rm -f $E/logs/02_load_pg*_osd*.log   # force reloads under the new cluster
$E/scripts/10_all.sh
echo "[$(date -u +%FT%TZ)] === fp32 vec redo done ==="
