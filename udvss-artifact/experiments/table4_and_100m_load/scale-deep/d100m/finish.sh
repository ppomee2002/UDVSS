#!/bin/bash
# 2026-10-12 08:14Z the OSD was OOM-killed during M192 r1 (C=80): the bench client
# grew to ~60 GB holding 80 x 1024 x 192 per-PG results, + OSD 63 GB > host RAM.
# Restart (journal replay) with warm-up, finish M4 x3 at the chosen C=80, skip M192
# (M128 already reaches recall 0.9925), then release the Milvus chain.
R=/home/dev_path/experiments/scale-deep; D=$R/d100m; S=$R/ceph/scripts; ARM=d100m_pg16384
export SEASTORE_DEVICE_SIZE=224G BENCH_PROBE_BUDGET=256
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $D/driver.log >> $R/chain.log; }
log "OOM-kill of crimson-osd at 08:14Z during M192 r1 (client ~60 GB at M192/C80 + OSD 63 GB); M192 skipped; restarting OSD for M4"
mkdir -p $R/ceph/results/$ARM/failed; mv $R/ceph/results/$ARM/m192_* $R/ceph/results/$ARM/failed/ 2>/dev/null
if bash $S/activate_workload.sh $ARM --skip-count > $R/ceph/logs/${ARM}_activate_after_oom.log 2>&1; then
  log "OSD restarted: $(tail -1 $R/ceph/logs/${ARM}_activate_after_oom.log | cut -c1-120)"
  for r in 1 2 3; do
    out=$(bash $S/run_one.sh $ARM 4 1024 80 r$r 2>&1 | tail -2 | head -1)
    echo "  [$ARM] $out" >> $R/ceph/logs/run_all.log
    log "M4 r$r: $(echo "$out" | grep -oE 'qps=[0-9.]+|recall=[0-9.]+|cand=[0-9.]+|failures=[0-9]+' | tr '\n' ' ')"
  done
else log "FAILED: OSD restart after OOM (see ${ARM}_activate_after_oom.log)"; fi
for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
sleep 30; for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill -9 $p; done
log "OSD stopped (cluster kept)"; log "=== D100M DONE ==="
