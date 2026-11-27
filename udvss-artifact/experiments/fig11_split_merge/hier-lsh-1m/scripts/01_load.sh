#!/bin/bash
# Pool (1024 PGs) + hierarchical index load, then the object list for full-PG scans.
source $HOME/hier/scripts/env.sh
set -e
ceph_ osd pool create $POOL 1024 1024 >/dev/null
ceph_ osd pool set $POOL pg_autoscale_mode off >/dev/null
ceph_ osd pool application enable $POOL rados >/dev/null
for t in $(seq 1 360); do
  n=$(ceph_ pg ls-by-pool $POOL -f json | python3 -c 'import json,sys;d=json.load(sys.stdin);d=d.get("pg_stats",d);print(sum(1 for p in d if p["state"]=="active+clean"))' || echo 0)
  [ "$n" = 1024 ] && break; sleep 5
done
log "active+clean PGs: $n"; [ "$n" = 1024 ] || exit 1
log "=== load SIFT1M (hierarchical S=$HSCALE) ==="
bench --operation load --pool $POOL --index $INDEX $IDXARGS $DSET \
  --query-limit 1000 --load-concurrency 64 --top-k 10 > $H/logs/01_load.log 2>&1 \
  || { log "FATAL: load"; tail -5 $H/logs/01_load.log; exit 1; }
grep -E "^LOAD" $H/logs/01_load.log | cut -c1-400
$OB/rados -c $CONF -p $POOL ls 2>/dev/null | sort > $H/results/$POOL.objs
log "objects listed: $(wc -l < $H/results/$POOL.objs)"
log "=== 01 complete ==="
