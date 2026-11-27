#!/bin/bash
# Usage: 10_load.sh 1m|10m|100m -- fresh pool ws_lsh_pg1024_<n>, SQ8, 1 OSD.
source /home/dev_path/experiments/workload-scale-sq8/scripts/env_ws.sh
set -e
S=$1; POOL=$(ws_pool $S); mkdir -p $RES/logs
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
cd $SQ_SRC/build
taskset -c $CLIENT_CPUS $SQ_BIN/ceph-vector-bench-native --conf $CEPH_CONF --operation load \
  --pool $POOL --index ${POOL}_index $(sq_idxargs $WS_PG) $QUANT $(ws_dset $S) --query-limit 1000 \
  --load-concurrency 32 --top-k 10 > $RES/logs/load_$S.log 2>&1
grep -E "^LOAD" $RES/logs/load_$S.log | cut -c1-300
ceph_ df | grep -E "$POOL|RAW|TOTAL" | tee $RES/logs/df_$S.txt
log "=== load $S done ==="
