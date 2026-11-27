#!/bin/bash
# Unattended pipeline: per scale, wait for its groundtruth on 109 (and the full
# base copy for 100m), fetch it, then 20_all.sh for that scale. Analyze at the end.
source /home/dev_path/experiments/workload-scale-sq8/scripts/env_ws.sh
mkdir -p $RES
for S in 1m 10m 100m; do
  until ssh -o BatchMode=yes $OSD_HOST "grep -q '^$S done' /home/ubuntu/deep1b/gt.log"; do
    ssh -o BatchMode=yes $OSD_HOST "grep -q Traceback /home/ubuntu/deep1b/gt.log" && { log "FATAL: GT failed"; exit 1; }
    sleep 60; done
  [ -s $DDIR/deep${S}_gt_q1000.ivecs ] || scp -q $OSD_HOST:/home/ubuntu/deep1b/deep${S}_gt_q1000.ivecs $DDIR/
  need=$(( $(ws_n $S) * 388 ))
  until [ "$(stat -c %s $DDIR/deep100m_base.fvecs)" -ge $need ]; do sleep 60; done
  log "=== scale $S ==="
  WS_SCALES=$S $WS/scripts/20_all.sh || { log "FATAL: scale $S"; exit 1; }
done
python3 $WS/scripts/analyze.py
log "=== RUN_ALL DONE ==="
