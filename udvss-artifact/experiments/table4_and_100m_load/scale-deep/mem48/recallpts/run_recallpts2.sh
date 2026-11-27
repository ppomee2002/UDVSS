#!/bin/bash
# 2026-10-17 user: Ceph mem48 recall 0.90 / 0.95 points on the live d100m_m48noomap cluster
# (same settings as ceph_noomap_measure.sh: C=20, batch 1024, 120 s, 1 rep), while Vespa mem40
# feeds on other cores. M16/M32 are re-measured as anchors to check interference with the feed.
# Client pinned to 24-35 (default 8-47 overlaps the Vespa feeder 9-11,20-23,44-47 and Vespa 12-19).
R=/home/dev_path/experiments/scale-deep; D=$R/mem48/recallpts; S=$R/ceph/scripts
ARM=d100m_m48noomap; CONF=/vstart-extra/sd-d100m-m48noomap/ceph.conf
BIN=/home/dev_path/ceph-vectornode-seastore-batch/build/bin
export SEASTORE_DEVICE_SIZE=224G BENCH_PROBE_BUDGET=256 OSD_CG=/sys/fs/cgroup/osd48 CRIMSON_VECTOR_NO_OMAP_IDENTITY=1
unset CLIENT_CORES   # default 8-47, same as the 2026-10-16 sweep (Vespa stopped)
PRE_GB=5.2; ABORT_GB=5.4; C=20
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $D/run2.log; }
shard_max() { timeout 20 $BIN/ceph -c $CONF daemon osd.0 dump_metrics memory_allocated_memory 2>/dev/null | python3 -c "
import json,sys
try:
  d=json.load(sys.stdin)['metrics']; print('%.3f' % max(m['memory_allocated_memory']['value']/2**30 for m in d if 'memory_allocated_memory' in m))
except Exception: print('NA')"; }
clients() { ps -eo pid,comm | awk '$2 ~ /^ceph-vector-ben/ {print $1}'; }
osd_alive() { kill -0 3457008 2>/dev/null; }
log "start shardmax=$(shard_max)G"
for M in 32 36 40; do
  sm=$(shard_max)
  if [ "$sm" != NA ] && awk -v a=$sm -v t=$PRE_GB 'BEGIN{exit !(a>t)}'; then log "stop: shard max ${sm}G > $PRE_GB before M$M"; break; fi
  bash $S/run_one.sh $ARM $M 1024 $C rp2 > $D/run2_m$M.out 2>&1 & rp=$!
  while kill -0 $rp 2>/dev/null; do
    sm=$(shard_max)
    if [ "$sm" != NA ] && awk -v a=$sm -v t=$ABORT_GB 'BEGIN{exit !(a>t)}'; then log "M$M abort: shard ${sm}G"; for q in $(clients); do kill $q; done; fi
    osd_alive || break; sleep 3; done
  wait $rp
  log "M$M: $(tail -2 $D/run2_m$M.out | head -1 | grep -oE 'OK|FAILED|qps=[0-9.]+|recall=[0-9.]+|failures=[0-9]+' | tr '\n' ' ') shardmax=$(shard_max)G"
  osd_alive || { log "OSD died"; break; }
done
log "=== RECALLPTS2 DONE ==="
