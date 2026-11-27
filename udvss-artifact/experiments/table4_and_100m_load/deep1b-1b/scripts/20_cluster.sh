#!/bin/bash
# One crimson OSD (8 reactors, cores 0-7, NO_OMAP identity), 600G SeaStore
# device, pool of $PG PGs, all active+clean. Same steps as scale-deep
# create_and_load.sh plus the mem48 fixes set BEFORE any data lands.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
set -e
[ -e $V ] && die "20: $V exists"
mkdir -p $V; cd $BLD
log "20: vstart (device $DEV_SIZE, port $PORT)"
VSTART_DEST=$V CEPH_CONF=$CONF CEPH_PORT=$PORT MON=1 OSD=1 MGR=1 MDS=0 RGW=0 \
  $SRC/src/vstart.sh -n -X --crimson --seastore --crimson-smp 8 \
  --seastore-device-size $DEV_SIZE --without-dashboard \
  -o osd_pool_default_size=1 -o osd_pool_default_min_size=1 > $X/logs/vstart.log 2>&1 || die "vstart"
python3 - $CONF <<'PY'
import sys
p=sys.argv[1]; out=[]
for l in open(p).read().split('\n'):
    out.append(l)
    if l.strip() in ('[global]','[osd]','[client]','[mon]','[mgr]'):
        out.append('        seastore_experimental_vector_node=true')
        out.append('        seastore_max_concurrent_transactions = 256')
open(p,'w').write('\n'.join(out))
PY
sed -i 's/mon_max_pg_per_osd = 1000/mon_max_pg_per_osd = 131072/' $CONF
for i in $(seq 1 30); do
  ceph_ config set global mon_max_pg_per_osd 131072
  ceph_ tell mon.a config set mon_max_pg_per_osd 131072 >/dev/null
  [ "$(ceph_ config get mon mon_max_pg_per_osd)" = 131072 ] && break; sleep 5
done
[ "$(ceph_ config get mon mon_max_pg_per_osd)" = 131072 ] || die "mon_max_pg_per_osd"
ceph_ config set osd.0 crimson_cpu_set $OSD_CORES
ceph_ config set osd osd_pg_log_dups_tracked 100
ceph_ config set osd osd_min_pg_log_entries 50
ceph_ config set mgr mgr_max_pg_num_change 65536   # PG creation pace only (3-host run: 35 min -> 45 s)
ceph_ config set mgr mgr_max_pg_creating 65536
ceph_ config set osd debug_osd 0/0   # 2026-10-22: INFO RenewLease lines of 65536 PGs = 1.8 MB/s of log
log "20: conf: vector node, txn 256, mon_max_pg_per_osd 131072, cpu_set $OSD_CORES, pg log dups 100 / min 50, debug_osd 0/0"
P=$(osd_pid); [ -n "$P" ] && { for t in /proc/$P/task/*; do taskset -pc 0-47 ${t##*/} >/dev/null 2>&1; done; kill $P; }
for i in $(seq 1 60); do [ -z "$(osd_pid)" ] && break; sleep 2; done
[ -z "$(osd_pid)" ] || kill -9 $(osd_pid)
setsid bash -c "ulimit -l unlimited; exec env CRIMSON_VECTOR_NO_OMAP_IDENTITY=1 $BIN/crimson-osd -i 0 -c $CONF -f" \
  >> $X/logs/osd_stdout.log 2>&1 < /dev/null &
for i in $(seq 1 60); do sleep 5; ceph_ -s | grep -q "1 osds: 1 up" && break; done
P=$(osd_pid); [ -n "$P" ] || die "osd not up"
log "20: osd pid $P noomap=$(tr '\0' '\n' < /proc/$P/environ | grep -c NO_OMAP) dups=$(ceph_ daemon osd.0 config get osd_pg_log_dups_tracked | tr -d ' \n')"
ceph_ osd pool create $POOL $PG $PG | tail -1
for i in $(seq 1 1440); do   # 65536 PGs are created in throttled batches
  S=$(ceph_ pg stat | tail -1); echo "$S" | grep -q "$PG pgs: $PG active+clean" && break
  [ $((i % 60)) = 0 ] && log "20: $S"
  [ -n "$(osd_pid)" ] || die "osd died during PG creation"
  sleep 5
done
echo "$S" | grep -q "$PG pgs: $PG active+clean" || die "pool not clean: $S"
log "20: $S"
log "=== 20 complete ==="
