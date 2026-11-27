#!/bin/bash
# Usage: 02_pool_load.sh PG -- create pool sqn_pg<PG> (size 1), pin PG i to
# osd.(i mod N) with pg-upmap BEFORE any data lands, then load deep1m.
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env.sh
set -e
PG=$1
POOL=sqn_pg$PG
N=$(ceph_ osd stat -f json | python3 -c 'import json,sys;print(json.load(sys.stdin)["num_up_osds"])')
ceph_ config set mon mon_allow_pool_delete true
ceph_ balancer off 2>/dev/null || true
ceph_ osd set-require-min-compat-client luminous --yes-i-really-mean-it >/dev/null 2>&1 || true
ceph_ osd pool rm $POOL $POOL --yes-i-really-really-mean-it >/dev/null 2>&1 || true
ceph_ osd pool create $POOL $PG $PG --size 1 --autoscale-mode off
ceph_ osd pool set $POOL min_size 1
POOLID=$(ceph_ osd pool ls detail -f json | python3 -c "import json,sys;print([p['pool_id'] for p in json.load(sys.stdin) if p['pool_name']=='$POOL'][0])")
for i in $(seq 0 $((PG-1))); do
  ceph_ osd pg-upmap $POOLID.$(printf %x $i) $(( i % N )) >/dev/null
done
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
  | tee $EXP/logs/02_placement_pg${PG}_osd${N}.txt
log "load deep1m into $POOL (pg_num $PG, $N OSDs)"
cd $BUILD
taskset -c $CLIENT_CPUS $BIN/ceph-vector-bench-native --conf $CEPH_CONF --operation load \
  --pool $POOL --index ${POOL}_index $(idxargs $PG) $DSET --query-limit 1000 \
  --load-concurrency 32 --top-k 10 > $EXP/logs/02_load_pg${PG}_osd${N}.log 2>&1
grep -E "^LOAD" $EXP/logs/02_load_pg${PG}_osd${N}.log | cut -c1-300
ceph_ df | grep -E "POOL|$POOL"
log "=== 02 complete (PG=$PG N=$N) ==="
