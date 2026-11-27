#!/bin/bash
# mon + mgr on 109 (vstart, native build), no OSDs. OSDs come from 01_osds.sh.
source $HOME/osdscale/scripts/env.sh
set -e
mkdir -p $X/logs $X/results
[ -n "$(mon_pids)" ] && die "mon/mgr already running: $(mon_pids)"
rm -rf $VDEST; mkdir -p $VDEST
cd $SRC/build
VSTART_DEST=$VDEST CEPH_CONF=$CONF CEPH_PORT=$PORT MON=1 OSD=0 MGR=1 MDS=0 RGW=0 \
  taskset -c $MON_CPUS $SRC/src/vstart.sh -n -X --crimson --seastore -i $IP --without-dashboard \
  > $X/logs/00_vstart.log 2>&1
log "vstart done"
ceph_ config set global seastore_experimental_vector_node true
ceph_ config set global mon_allow_pool_size_one true
ceph_ config set global osd_pool_default_size 1
ceph_ config set global mon_warn_on_pool_no_redundancy false
ceph_ config set global osd_pool_default_pg_autoscale_mode off
# N=1 holds all 16384 PGs of the 100M pool; the conf-file 1000 wins over the
# config DB, so patch the file and inject into the running mon as well.
sed -i 's/mon_max_pg_per_osd = 1000/mon_max_pg_per_osd = 16384/' $CONF
ceph_ config set global mon_max_pg_per_osd 16384
ceph_ tell mon.a config set mon_max_pg_per_osd 16384 >/dev/null
# mem48 root cause (2026-10-16): 16384 PGs x 3000 PG-log dups ran the OSD out of
# memory at 100M. Same fix as mem48 (official options), set before any load.
ceph_ config set osd osd_pg_log_dups_tracked 100
ceph_ config set osd osd_min_pg_log_entries 50
ceph_ config set osd ms_bind_port_min $OSD_PORT_MIN
ceph_ config set osd ms_bind_port_max $OSD_PORT_MAX
ceph_ config get mon mon_max_pg_per_osd
ceph_ -s | head -12
log "=== 00 complete ==="
