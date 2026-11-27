#!/bin/bash
# Usage: 02_load.sh <scale> -- create the pool (size 1), pin PG i to
# osd.(i mod N) with pg-upmap BEFORE any data lands, load once, then prove the
# load with a full-PG entry count. Fail-stop.
source $HOME/osdscale/scripts/env.sh
set -e
scale_vars $1
N=$(num_up)
R=$X/results/$SCALE/osd$N; mkdir -p $R
ceph_ balancer off || true
ceph_ osd set-require-min-compat-client luminous --yes-i-really-mean-it >/dev/null || true
ceph_ osd pool create $POOL $PG $PG --size 1 --autoscale-mode off
ceph_ osd pool set $POOL min_size 1 >/dev/null
POOLID=$(ceph_ osd pool ls detail -f json | python3 -c "import json,sys;print([p['pool_id'] for p in json.load(sys.stdin) if p['pool_name']=='$POOL'][0])")
if [ $N -gt 1 ]; then
  log "pg-upmap $PG PGs -> osd (i mod $N)"
  # some calls fail while PGs are still being created (xargs then exits 123):
  # the check loop below re-issues the upmap for every PG not on its target
  for i in $(seq 0 $((PG-1))); do echo "$POOLID.$(printf %x $i) $(( i % N ))"; done \
    | xargs -P 16 -n 2 $OB/ceph -c $CONF osd pg-upmap >/dev/null 2>&1 || true
fi
for t in $(seq 1 720); do   # 16384 PGs are created in throttled batches
  ceph_ pg ls-by-pool $POOL -f json | python3 -c "
import json,sys
d=json.load(sys.stdin); d=d['pg_stats'] if isinstance(d,dict) else d
seen={int(p['pgid'].split('.')[1],16):p for p in d}
for i in range($PG):
    p=seen.get(i)
    if p is None or p['acting_primary']!=i%$N or 'active+clean' not in p['state']:
        print('$POOLID.%x %d %s' % (i, i%$N, 'missing' if p is None else p['state']))" > $R/pg_bad.txt || true
  bad=$(wc -l < $R/pg_bad.txt)
  [ "$bad" = 0 ] && break
  if [ $N -gt 1 ] && [ $((t % 6)) = 0 ]; then
    log "  $bad PGs not active+clean on target; re-issuing their upmaps"
    grep -v missing $R/pg_bad.txt | awk -v n=$N '{print $1, $2}' \
      | xargs -P 16 -n 2 $OB/ceph -c $CONF osd pg-upmap >/dev/null 2>&1 || true
  fi
  sleep 5
done
[ "$bad" = 0 ] || die "$bad PGs not active+clean on their upmap target"
ceph_ pg ls-by-pool $POOL -f json | python3 -c "
import json,sys,collections
d=json.load(sys.stdin); d=d['pg_stats'] if isinstance(d,dict) else d
print('PGs per OSD:', dict(sorted(collections.Counter(p['acting_primary'] for p in d).items())))" | tee $R/placement.txt

log "load $SCALE ($BASE_COUNT) into $POOL, $N OSDs, conc $LOAD_CONC"
LL=$R/load.log
S=$(date +%s)
bench --operation load --pool $POOL --index $INDEX $IDXARGS $DSET --load-concurrency $LOAD_CONC > $LL 2>&1 \
  || { tail -5 $LL; die "load exited non-zero"; }
echo "load_seconds=$(( $(date +%s) - S ))" >> $LL
grep -aE "^PIVOT_MODEL|^PIVOT_PLACEMENT|^LOAD " $LL | cut -c1-300
f() { grep -aoE "$1=[^ ]+" $LL | tail -1 | cut -d= -f2; }
[ "$(f completed)" = "$BASE_COUNT" ] || die "completed=$(f completed) expected $BASE_COUNT"
[ "$(f failures)" = "0" ] || die "failures=$(f failures)"

log "full-PG entry count"
CL=$R/count.log
bench --operation fanout-query --pool $POOL --index $INDEX $IDXARGS $DSET \
  --m $PG --pivot-probe-budget $PG --query-limit 10 --rounds 1 --query-concurrency 1 \
  --fanout-mode parallel --query-batch-size 1 --warmup-rounds 0 > $CL 2>&1 || die "full-PG scan failed"
L=$(grep -a "^FANOUT_QUERY" $CL | tail -1)
g() { echo "$L" | tr ' ' '\n' | grep "^$1=" | cut -d= -f2; }
REC=$(g avg_total_matching_records_per_query); REC=${REC%%.*}
DUPMAX=$(( BASE_COUNT / 10000 > 100 ? BASE_COUNT / 10000 : 100 ))
log "records=$REC (expect $BASE_COUNT) duplicate_entries=$(g duplicate_entries) failures=$(g failures)"
[ "$REC" = "$BASE_COUNT" ] || die "entry count $REC != $BASE_COUNT"
[ "$(g duplicate_entries)" -le $DUPMAX ] || die "duplicate_entries=$(g duplicate_entries) > $DUPMAX"
ceph_ df | grep -E "POOL|$POOL"
echo "LOADED_OK $SCALE osd$N"
