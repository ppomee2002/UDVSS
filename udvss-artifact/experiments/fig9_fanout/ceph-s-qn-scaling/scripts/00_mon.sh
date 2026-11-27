#!/bin/bash
# mon + mgr on 94 (no OSDs). OSDs are added on 109 by 01_osds.sh.
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env.sh
set -e
[ -n "$(exp_pids)" ] && { log "FATAL: cluster already running"; exit 1; }
rm -rf "$VDEST"; mkdir -p "$VDEST" $EXP/logs
cd $BUILD
VSTART_DEST=$VDEST CEPH_CONF=$VDEST/ceph.conf CEPH_PORT=$CEPH_PORT \
  MON=1 OSD=0 MGR=1 MDS=0 RGW=0 \
  $SRC/src/vstart.sh -n -X --crimson --seastore -i $MON_IP --without-dashboard \
  > $EXP/logs/00_vstart.log 2>&1
log "vstart done"
grep -q "mon host" $CEPH_CONF
ceph_ config set global seastore_experimental_vector_node true
ceph_ config set global mon_allow_pool_size_one true
ceph_ config set global osd_pool_default_size 1
ceph_ config set global mon_warn_on_pool_no_redundancy false
ceph_ config set osd ms_bind_port_min $OSD_PORT_MIN
ceph_ config set osd ms_bind_port_max $OSD_PORT_MAX
ceph_ config set osd seastore_device_size $OSD_DEV_SIZE
ceph_ -s | head -12
log "=== 00 complete ==="
