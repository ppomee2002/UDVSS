#!/bin/bash
# Usage: 10_load.sh 1m|10m|100m -- fresh pool, SQ8, 1 OSD (as workload-scale-sq8 10_load.sh,
# + --lsh-aligned-locators), then the object list a full PG scan needs.
source /home/dev_path/experiments/paper-100m/ws/scripts/env.sh
set -e
S=$1; POOL=$(ws_pool $S); mkdir -p $RES/logs $RES/objs
ceph_ config set mon mon_allow_pool_delete true
for p in $(ceph_ osd pool ls); do ceph_ osd pool rm $p $p --yes-i-really-really-mean-it >/dev/null; done
ceph_ osd pool create $POOL $WS_PG $WS_PG --size 1 --autoscale-mode off
ceph_ osd pool set $POOL min_size 1
for t in $(seq 1 120); do
  n=$(ceph_ pg ls-by-pool $POOL -f json | python3 -c "
import json,sys; d=json.load(sys.stdin); d=d['pg_stats'] if isinstance(d,dict) else d
print(sum('active+clean' in p['state'] for p in d))")
  [ "$n" = $WS_PG ] && break; sleep 5
done
[ "$n" = $WS_PG ] || { log "FATAL: $n/$WS_PG active+clean"; exit 1; }
log "load deep1b $S into $POOL"
echo "[$(date -u +%FT%TZ)] load $S: $BB/ceph-vector-bench-native --operation load --pool $POOL $(ws_idxargs $WS_PG) $QUANT $(ws_dset $S)" >> $RES/logs/commands.log
taskset -c $CLIENT_CPUS $BB/ceph-vector-bench-native --conf $CEPH_CONF --operation load \
  --pool $POOL --index ${POOL}_index $(ws_idxargs $WS_PG) $QUANT $(ws_dset $S) --query-limit 1000 \
  --load-concurrency 32 --top-k 10 > $RES/logs/load_$S.log 2>&1 \
  || { log "FATAL: load $S"; tail -5 $RES/logs/load_$S.log; exit 1; }
grep -E "^LOAD" $RES/logs/load_$S.log | cut -c1-300
grep -qE "^LOAD.* failures=0( |$)" $RES/logs/load_$S.log || { log "FATAL: load failures"; exit 1; }
ceph_ df | grep -E "$POOL|RAW|TOTAL" | tee $RES/logs/df_$S.txt
$BIN/rados -c $CEPH_CONF -p $POOL ls 2>/dev/null | sort > $RES/objs/$S.objs
log "objects listed: $(wc -l < $RES/objs/$S.objs)"
log "=== load $S done ==="
