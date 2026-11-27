#!/bin/bash
# Usage: 01_osds.sh <scale> <N> -- drop every pool, purge every OSD and delete
# its device, then create N fresh OSDs (6 reactors each, see osd_cpus).
source $HOME/osdscale/scripts/env.sh
set -e
scale_vars $1; N=$2
[ -n "$(mon_pids)" ] || die "mon not running (00_mon.sh)"
MEM=$(osd_mem $N); DEV=$(osd_dev $N)
log "scale=$SCALE N=$N mem/osd=$MEM dev/osd=$DEV reactors/osd=$RPO"

ceph_ config set mon mon_allow_pool_delete true
for p in $(ceph_ osd pool ls); do ceph_ osd pool rm $p $p --yes-i-really-really-mean-it >/dev/null || true; done
for p in $(osd_pids); do
  for t in /proc/$p/task/*; do taskset -pc 0-127 ${t##*/} >/dev/null 2>&1 || true; done
  kill -TERM $p 2>/dev/null || true
done
for t in $(seq 1 60); do [ -z "$(osd_pids)" ] && break; sleep 2; done
for p in $(osd_pids); do log "SIGKILL wedged osd pid $p"; kill -9 $p || true; done
for t in $(seq 1 30); do [ -z "$(osd_pids)" ] && break; sleep 1; done
[ -z "$(osd_pids)" ] || die "OSDs still running"
for id in $(ceph_ osd ls); do
  ceph_ osd down $id >/dev/null || true
  ceph_ osd purge $id --force --yes-i-really-mean-it >/dev/null || true
done
[ -z "$(ceph_ osd ls)" ] || die "OSDs left after purge: $(ceph_ osd ls)"
rm -rf $ODIR; mkdir -p $ODIR/dev $ODIR/out $ODIR/asok

# Fresh mon/mgr for every N: thousands of pg-upmap epochs make the mon trim
# old osdmaps, and a new crimson OSD then aborts at boot ("MOSDMap lied about
# what maps it had", ENOENT) -- seen at 10m N=8 after the N=4 upmaps.
for p in $(mon_pids); do kill $p 2>/dev/null || true; done
for t in $(seq 1 30); do [ -z "$(mon_pids)" ] && break; sleep 1; done
for p in $(mon_pids); do kill -9 $p 2>/dev/null || true; done
bash $X/scripts/00_mon.sh > $X/logs/00_mon_${SCALE}_osd${N}.log 2>&1 || die "00_mon failed"
log "fresh mon/mgr"

FSID=$(ceph_ fsid)
MONHOST=$(grep -oP "mon host\s*=\s*\K.*" $CONF | tail -1)
cat > $ODIR/ceph.conf <<EOC
[global]
        fsid = $FSID
        mon host = $MONHOST
        auth cluster required = none
        auth service required = none
        auth client required = none
        ms bind msgr2 = true
        ms bind msgr1 = false
        ms mon client mode = crc
        enable experimental unrecoverable data corrupting features = *
        seastore_experimental_vector_node = true
        osd_pool_default_pg_autoscale_mode = off
[osd]
        osd data = $ODIR/dev/osd\$id
        log file = $ODIR/out/\$name.log
        admin socket = $ODIR/asok/\$name.asok
        osd class dir = $BUNDLE/lib
        osd objectstore = seastore
        seastore device size = $DEV
        seastore_max_concurrent_transactions = 256
        public addr = $IP
        cluster addr = $IP
        osd_check_max_object_name_len_on_startup = false
        osd fast shutdown = false
EOC
ceph_ config set osd crimson_memory $MEM
ulimit -n 1048576
for i in $(seq 0 $((N-1))); do
  uuid=$(uuidgen)
  id=$(ceph_ osd new $uuid)
  [ "$id" = "$i" ] || die "expected osd.$i got $id"
  ceph_ config set osd.$i crimson_cpu_set $(osd_cpus $i)
  mkdir -p $ODIR/dev/osd$i
  $BUNDLE/run.sh crimson-osd -i $i -c $ODIR/ceph.conf --mkfs --osd-uuid $uuid > $ODIR/out/mkfs.$i.log 2>&1 \
    || { tail -20 $ODIR/out/mkfs.$i.log; die "mkfs osd.$i"; }
  # NO_OMAP identity: skip the per-vector OMAP identity key (mem48 100M setting).
  (cd $ODIR && CRIMSON_VECTOR_NO_OMAP_IDENTITY=1 setsid -f $BUNDLE/run.sh crimson-osd -i $i -c $ODIR/ceph.conf -f \
    > $ODIR/out/osd.$i.stdout 2>&1 < /dev/null)
  log "osd.$i started on cpus $(osd_cpus $i)"
done
for t in $(seq 1 120); do
  up=$(num_up); [ "$up" = "$N" ] && break; sleep 5
done
[ "$up" = "$N" ] || die "only $up/$N OSDs up"
log "$N/$N OSDs up"
for p in $(osd_pids); do
  printf "  pid %s threads=%s env_noomap=%s\n" $p $(ls /proc/$p/task | wc -l) \
    "$(tr '\0' '\n' < /proc/$p/environ | grep -c CRIMSON_VECTOR_NO_OMAP_IDENTITY=1)"
done
echo -e "$(date -u +%FT%TZ)\t$SCALE\t$N\t$MEM\t$DEV\t$RPO" >> $X/results/osd_configs.tsv
log "=== 01 complete (scale=$SCALE N=$N) ==="
