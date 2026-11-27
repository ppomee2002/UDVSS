#!/bin/bash
# Deep1B 1B chain: wait for the base download -> 00 prep/checks -> 10 place ->
# 20 cluster -> 30 load -> 40 count -> 50 measure. Marker "=== 1B ALL DONE ===".
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
log "all: waiting for $BASE ($BASE_BYTES B)"
until [ "$(stat -c %s $BASE 2>/dev/null)" = "$BASE_BYTES" ] && { [ "${MINI:-0}" = 1 ] || ! ps -eo args | grep -q "[w]get .*base.1B.fbin"; }; do sleep 120; done
log "all: download complete"
[ -s $QUERY ] && [ -s $GT ] || python3 -I $S/00_prep.py /data1b/deep1b $RUN >> $LOG 2>&1 || die "00_prep failed"
log "all: 00 ok"
bash $S/10_place.sh || die "10 failed"
[ -e $V ] || bash $S/20_cluster.sh || die "20 failed"
ps -eo args | grep -q "[d]eep1b-1b/scripts/watch.sh" || { nohup setsid bash $S/watch.sh > $X/logs/watch.out 2>&1 < /dev/null & }
bash $S/30_load.sh || die "30 stopped (rc=$?)"
grep -q LOADED_OK $X/logs/count.log 2>/dev/null || bash $S/40_count.sh || die "40 failed"
bash $S/50_measure.sh || die "50 failed"
log "=== 1B ALL DONE ==="
