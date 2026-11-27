#!/bin/bash
# Usage: 01_osds.sh N -- (re)create N fresh crimson/SeaStore OSDs on 109.
# Every existing OSD of this cluster is stopped and purged first, so each
# OSD count starts from empty devices.
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env.sh
set -e
N=$1
[ -n "$(exp_pids)" ] || { log "FATAL: mon not running (00_mon.sh)"; exit 1; }

# seastar reserves ~11k aio events per reactor; 16 OSDs x 4 reactors exceed
# the 65536 default. Raised once, value recorded for restore.
# (ulimit -n is raised for mkfs/start: a 126-reactor OSD needs more than the
# ssh session default of 1024 fds and dies silently right after seastar starts.)
# CPU_POOL=all: each OSD gets its share of the 126-CPU pool (osd_cpus_pool);
# memory and device grow with the reactor count (0.5G and 8G per reactor,
# floors 16G / 64G) so a 126-reactor OSD is not starved per shard.
if [ "${CPU_POOL:-}" = all ]; then
  cpus_of() { osd_cpus_pool $1 $N; }
  maxc=$(cpus_of 0 | tr , '\n' | wc -l)
  OSD_MEM=$(( maxc / 2 > 16 ? maxc / 2 : 16 ))G
  # 8G of device per reactor: at 2G/reactor (64G, 32 reactors) a shard ran
  # out of journal segments while loading ("SegmentCleaner::allocate_segment:
  # out of space"); the 4-CPU runs had 16G/reactor
  OSD_DEV_SIZE=$(( maxc * 8 > 64 ? maxc * 8 : 64 ))G
  AIO_WANT=4194304
else
  cpus_of() { osd_cpus $1; }
  AIO_WANT=1048576
fi
log "N=$N cpu_pool=${CPU_POOL:-per-osd-4} mem=$OSD_MEM dev=$OSD_DEV_SIZE"
cur=$(r109 cat /proc/sys/fs/aio-max-nr)
if [ "$cur" -lt $AIO_WANT ]; then
  [ -f $EXP/logs/109_aio_max_nr.orig ] || echo "$cur" > $EXP/logs/109_aio_max_nr.orig
  r109 sudo sysctl -q -w fs.aio-max-nr=$AIO_WANT
fi

log "dropping pools, stopping/purging existing OSDs"
ceph_ config set mon mon_allow_pool_delete true
for p in $(ceph_ osd pool ls 2>/dev/null); do
  ceph_ osd pool rm $p $p --yes-i-really-really-mean-it >/dev/null 2>&1 || true
done
# [c] keeps the pattern from matching this ssh command's own shell.
r109 "pkill -9 -f '[b]undle[-a-z0-9]*/bin/crimson-osd -i' || true"
for t in $(seq 1 30); do [ "$(r109 "pgrep -f '[b]undle[-a-z0-9]*/bin/crimson-osd -i' | wc -l")" = 0 ] && break; sleep 1; done
# purge refuses an OSD the osdmap still shows up, so mark it down first
for id in $(ceph_ osd ls 2>/dev/null); do
  ceph_ osd down $id >/dev/null 2>&1 || true
  ceph_ osd purge $id --force --yes-i-really-mean-it >/dev/null 2>&1 || true
done
[ -z "$(ceph_ osd ls 2>/dev/null)" ] || { log "FATAL: OSDs left after purge: $(ceph_ osd ls)"; exit 1; }
for id in $(ceph_ osd crush ls ubuntu 2>/dev/null); do ceph_ osd crush rm $id || true; done

FSID=$(ceph_ fsid)
MONHOST=$(grep -oP "mon host\s*=\s*\K.*" $CEPH_CONF | tail -1)
r109 "rm -rf $RDIR/dev $RDIR/out $RDIR/asok; mkdir -p $RDIR/dev $RDIR/out $RDIR/asok"
r109 "cat > $RDIR/ceph.conf" <<EOF
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
        osd data = $RDIR/dev/osd\$id
        log file = $RDIR/out/\$name.log
        admin socket = $RDIR/asok/\$name.asok
        osd class dir = $RDIR/${BUNDLE:-bundle}/lib
        osd objectstore = seastore
        seastore device size = $OSD_DEV_SIZE
        public addr = $OSD_IP
        cluster addr = $OSD_IP
        osd_check_max_object_name_len_on_startup = false
        osd fast shutdown = false
EOF

ceph_ config set osd crimson_memory $OSD_MEM
for i in $(seq 0 $((N-1))); do
  uuid=$(uuidgen)
  id=$(ceph_ osd new $uuid)
  [ "$id" = "$i" ] || { log "FATAL: expected osd.$i got $id"; exit 1; }
  ceph_ config set osd.$i crimson_cpu_set $(cpus_of $i)
  r109 "ulimit -n 1048576; mkdir -p $RDIR/dev/osd$i && $RDIR/${BUNDLE:-bundle}/run.sh crimson-osd -i $i -c $RDIR/ceph.conf --mkfs --osd-uuid $uuid > $RDIR/out/mkfs.$i.log 2>&1" \
    || { log "FATAL: mkfs osd.$i"; r109 tail -20 $RDIR/out/mkfs.$i.log; exit 1; }
  # setsid -f: the ssh session must not stay attached to the daemon.
  r109 "ulimit -n 1048576; cd $RDIR && setsid -f $RDIR/${BUNDLE:-bundle}/run.sh crimson-osd -i $i -c $RDIR/ceph.conf -f > $RDIR/out/osd.$i.stdout 2>&1 < /dev/null"
  log "osd.$i started on $(cpus_of $i | tr , '\n' | wc -l) cpus: $(cpus_of $i | cut -c1-60)"
done

for t in $(seq 1 90); do
  up=$(ceph_ osd stat -f json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["num_up_osds"])')
  [ "$up" = "$N" ] && { log "$N/$N OSDs up after $((t*5))s"; break; }
  sleep 5
done
[ "$up" = "$N" ] || { log "FATAL: only $up/$N up"; exit 1; }
ceph_ osd tree | head -$((N+5))
log "=== 01 complete (N=$N) ==="
