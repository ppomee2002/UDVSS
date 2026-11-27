#!/bin/bash
# vstart (staged-prefix build) + swap in the new ceph-mon and crimson-osd.
source /home/dev_path/experiments/paper-100m/sm/scripts/env.sh
set -e
log "=== 00: vstart at $VDEST port $PORT ==="
[ -n "$(pids_of 'ceph-mon|ceph-mgr|crimson-osd')" ] && { log "FATAL: cluster already running"; exit 1; }
rm -rf "$VDEST"; mkdir -p "$VDEST"
ulimit -l unlimited
cd $OSD_SRC/build
VSTART_DEST=$VDEST CEPH_CONF=$VDEST/ceph.conf CEPH_PORT=$PORT \
  MON=1 OSD=1 MGR=1 MDS=0 RGW=0 \
  $OSD_SRC/src/vstart.sh -n -X --crimson --seastore --crimson-smp 8 \
  --seastore-device-size $DEV_SIZE --without-dashboard --nolockdep \
  -o osd_pool_default_size=1 -o osd_pool_default_min_size=1 \
  -o mon_warn_on_pool_no_redundancy=false \
  -o osd_pool_default_pg_autoscale_mode=off \
  -o mon_max_pg_per_osd=4096 \
  > $EXP/logs/00_vstart.log 2>&1
python3 - "$VDEST/ceph.conf" <<'PY'
import sys
p=sys.argv[1]; out=[]
for l in open(p).read().split('\n'):
    out.append(l)
    if l.strip() in ('[global]','[osd]','[client]','[mon]','[mgr]'):
        out.append('        seastore_experimental_vector_node=true')
    if l.strip() == '[global]':
        out.append('        crimson_allow_pg_merge=true')
open(p,'w').write('\n'.join(out))
PY
# swap mon
for p in $(pids_of ceph-mon); do kill -TERM $p; done
for i in $(seq 1 12); do sleep 2; [ -z "$(pids_of ceph-mon)" ] && break; done
bash -c 'exec nohup '"$MB"'/ceph-mon -i a -c '"$CONF"' -f' >> $EXP/logs/mon_stdout.log 2>&1 &
sleep 5
# swap osd
for p in $(osd_pids); do kill -TERM $p; done
for i in $(seq 1 24); do sleep 5; [ -z "$(osd_pids)" ] && break; done
for p in $(osd_pids); do kill -9 $p; done
bash -c 'ulimit -l unlimited; exec nohup '"$NB"'/crimson-osd -i 0 -c '"$CONF"' -f' \
  >> $EXP/logs/osd_stdout.log 2>&1 &
for i in $(seq 1 60); do
  sleep 5
  ceph_ -s | grep -qE "osd: 1 osds: 1 up" && break
done
ceph_ -s | grep -E "mon:|osd:|usage"
ceph_ -s | grep -qE "osd: 1 osds: 1 up" || { log "FATAL: osd not up"; exit 1; }
for p in $(pids_of 'ceph-mon|crimson-osd'); do echo "  $(tr '\0' ' ' < /proc/$p/cmdline | cut -c1-120)"; done
ceph_ config get mon crimson_allow_pg_merge
log "=== 00 complete ==="
