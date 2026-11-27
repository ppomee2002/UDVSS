#!/bin/bash
# Driver: for scale in SCALES (default 1m 10m 100m), for N in NS (1 4 8 12):
#   01_osds (fresh OSDs + devices) -> 02_load -> 03_measure.
# A failed step is logged and the driver moves to the next N (rows say FAILED).
# Log: logs/10_all.log; marker "=== OSDSCALE ALL DONE ===".
source $HOME/osdscale/scripts/env.sh
S=$X/scripts
SCALES=${SCALES:-"1m 10m 100m"}
NS=${NS:-"1 4 8 12"}
for sc in $SCALES; do
  for n in $NS; do
    log "##### $sc N=$n #####"
    L=$X/logs/${sc}_osd${n}
    bash $S/01_osds.sh $sc $n > $L.01.log 2>&1 || { log "01 FAILED ($sc N=$n), see $L.01.log"; tail -3 $L.01.log; continue; }
    bash $S/02_load.sh $sc > $L.02.log 2>&1   || { log "02 FAILED ($sc N=$n), see $L.02.log"; tail -3 $L.02.log; continue; }
    grep -aE "PIVOT_MODEL |PIVOT_PLACEMENT|^LOAD |records=" $L.02.log | cut -c1-200
    bash $S/03_measure.sh $sc > $L.03.log 2>&1 || { log "03 FAILED ($sc N=$n), see $L.03.log"; tail -3 $L.03.log; }
    grep -E "chosen C|M=(16|32) " $L.03.log
    free -g | sed -n 2p
  done
  log "===== $sc DONE ====="
done
log "=== OSDSCALE ALL DONE ==="
