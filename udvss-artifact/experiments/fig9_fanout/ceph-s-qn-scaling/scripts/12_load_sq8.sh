#!/bin/bash
# Usage: ROUTE=lsh|pivot 12_load_sq8.sh PG -- 02_pool_load.sh for the SQ8
# variant: pool sq8_<route>_pg<PG>, int8 vectors, pinned with pg-upmap.
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env_sq8.sh
set -e
mkdir -p $RES/logs
PG=$1
POOL=$(sq_pool $PG)
N=$(ceph_ osd stat -f json | python3 -c 'import json,sys;print(json.load(sys.stdin)["num_up_osds"])')
ceph_ config set mon mon_allow_pool_delete true
ceph_ balancer off 2>/dev/null || true
ceph_ osd set-require-min-compat-client luminous --yes-i-really-mean-it >/dev/null 2>&1 || true
ceph_ osd pool rm $POOL $POOL --yes-i-really-really-mean-it >/dev/null 2>&1 || true
ceph_ osd pool create $POOL $PG $PG --size 1 --autoscale-mode off
ceph_ osd pool set $POOL min_size 1
POOLID=$(ceph_ osd pool ls detail -f json | python3 -c "import json,sys;print([p['pool_id'] for p in json.load(sys.stdin) if p['pool_name']=='$POOL'][0])")
# 16 at a time: one ceph CLI call costs ~1 s, so 1024 sequential upmaps took ~15 min
for i in $(seq 0 $((PG-1))); do echo "$POOLID.$(printf %x $i) $(( i % N ))"; done \
  | xargs -P 16 -n 2 $BIN/ceph -c $CEPH_CONF osd pg-upmap >/dev/null 2>&1
for t in $(seq 1 60); do
  bad=$(ceph_ pg ls-by-pool $POOL -f json | python3 -c "
import json,sys
d=json.load(sys.stdin); d=d['pg_stats'] if isinstance(d,dict) else d
bad=[p['pgid'] for p in d if p['acting_primary']!=int(p['pgid'].split('.')[1],16)%$N or 'active+clean' not in p['state']]
print(len(bad))")
  [ "$bad" = 0 ] && break; sleep 5
done
[ "$bad" = 0 ] || { log "FATAL: $bad PGs not active+clean on their upmap target"; exit 1; }
ceph_ pg ls-by-pool $POOL -f json | python3 -c "
import json,sys,collections
d=json.load(sys.stdin); d=d['pg_stats'] if isinstance(d,dict) else d
print('PGs per OSD:', dict(sorted(collections.Counter(p['acting_primary'] for p in d).items())))" \
  | tee $RES/logs/placement_${POOL}_osd${N}.txt
log "load deep1m into $POOL (pg_num $PG, $N OSDs)"
cd $SQ_SRC/build
mkdir -p $RES/logs
taskset -c $CLIENT_CPUS $SQ_BIN/ceph-vector-bench-native --conf $CEPH_CONF --operation load \
  --pool $POOL --index ${POOL}_index $(sq_idxargs $PG) $QUANT $DSET --query-limit 1000 \
  --load-concurrency 32 --top-k 10 > $RES/logs/load_${POOL}_osd${N}.log 2>&1
grep -E "^LOAD" $RES/logs/load_${POOL}_osd${N}.log | cut -c1-300
grep -q "^LOAD completed=1000000 failures=0" $RES/logs/load_${POOL}_osd${N}.log || {
  log "FATAL: incomplete load, dropping $POOL"
  ceph_ osd pool rm $POOL $POOL --yes-i-really-really-mean-it; exit 1; }
ceph_ df | grep -E "POOL|$POOL"
log "=== 02 complete (PG=$PG N=$N) ==="
