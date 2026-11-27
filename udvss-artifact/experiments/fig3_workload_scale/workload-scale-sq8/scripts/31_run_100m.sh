#!/bin/bash
# 100m: load first (needs no groundtruth), then wait for the 100m GT, then the
# M sweep (full is skipped by 11_query.sh when 10m showed full == tree), analyze.
source /home/dev_path/experiments/workload-scale-sq8/scripts/env_ws.sh
S=100m
[ "$(stat -c %s $DDIR/deep100m_base.fvecs)" = 38800000000 ] || { log "FATAL: base copy incomplete"; exit 1; }
log "=== scale $S ==="
$WS/scripts/10_load.sh $S || { log "FATAL: load $S"; exit 1; }
until ssh -o BatchMode=yes $OSD_HOST "grep -q '^$S done' /home/ubuntu/deep1b/gt.log"; do
  ssh -o BatchMode=yes $OSD_HOST "grep -q Traceback /home/ubuntu/deep1b/gt.log" && { log "FATAL: GT failed"; exit 1; }
  sleep 60; done
scp -q $OSD_HOST:/home/ubuntu/deep1b/deep${S}_gt_q1000.ivecs $DDIR/ || { log "FATAL: GT fetch"; exit 1; }
for MODE in tree full; do for M in $WS_MS; do
  grep -q "^$S,[0-9]*,$MODE,1024,$M," $RES/runs.csv && continue
  $WS/scripts/11_query.sh $S $MODE $M || { log "FATAL: query $S $MODE $M"; exit 1; }
done; done
python3 $WS/scripts/analyze.py || { log "FATAL: analyze"; exit 1; }
log "=== RUN_ALL DONE ==="
