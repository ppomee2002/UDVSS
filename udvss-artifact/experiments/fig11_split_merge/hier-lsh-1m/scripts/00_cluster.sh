#!/bin/bash
# mon+mgr (vstart, native build) + one bundled crimson OSD, 32 reactors.
source $HOME/hier/scripts/env.sh
set -e
mkdir -p $H/logs $H/results
[ -n "$(exp_pids)" ] && { log "FATAL: cluster already running: $(exp_pids)"; exit 1; }
rm -rf $VDEST; mkdir -p $VDEST
cd $SRC/build
VSTART_DEST=$VDEST CEPH_CONF=$CONF CEPH_PORT=$PORT MON=1 OSD=0 MGR=1 MDS=0 RGW=0 \
  $SRC/src/vstart.sh -n -X --crimson --seastore -i $IP --without-dashboard \
  > $H/logs/00_vstart.log 2>&1
log "vstart done"
ceph_ config set global seastore_experimental_vector_node true
ceph_ config set global mon_allow_pool_size_one true
ceph_ config set global osd_pool_default_size 1
ceph_ config set global mon_warn_on_pool_no_redundancy false
ceph_ config set global osd_pool_default_pg_autoscale_mode off
ceph_ config set global mon_max_pg_per_osd 4096
ceph_ config set osd ms_bind_port_min 7910
ceph_ config set osd ms_bind_port_max 7999
ceph_ config set osd crimson_memory $OSD_MEM
FSID=$(ceph_ fsid)
MONHOST=$(grep -oP "mon host\s*=\s*\K.*" $CONF | tail -1)
mkdir -p $H/osd/dev $H/osd/out $H/osd/asok
cat > $H/osd/ceph.conf <<EOC
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
        osd data = $H/osd/dev/osd\$id
        log file = $H/osd/out/\$name.log
        admin socket = $H/osd/asok/\$name.asok
        osd class dir = $BUNDLE/lib
        osd objectstore = seastore
        seastore device size = $OSD_DEV_SIZE
        public addr = $IP
        cluster addr = $IP
        osd_check_max_object_name_len_on_startup = false
        osd fast shutdown = false
EOC
uuid=$(uuidgen)
id=$(ceph_ osd new $uuid)
[ "$id" = 0 ] || { log "FATAL: got osd.$id"; exit 1; }
ceph_ config set osd.0 crimson_cpu_set $OSD_CPUS
ulimit -n 1048576
mkdir -p $H/osd/dev/osd0
$BUNDLE/run.sh crimson-osd -i 0 -c $H/osd/ceph.conf --mkfs --osd-uuid $uuid > $H/osd/out/mkfs.0.log 2>&1 \
  || { log "FATAL: mkfs"; tail -20 $H/osd/out/mkfs.0.log; exit 1; }
cd $H/osd && setsid -f $BUNDLE/run.sh crimson-osd -i 0 -c $H/osd/ceph.conf -f > $H/osd/out/osd.0.stdout 2>&1 < /dev/null
for t in $(seq 1 90); do
  ceph_ osd stat | grep -q "1 up" && break; sleep 5
done
ceph_ osd stat | grep -q "1 up" || { log "FATAL: osd not up"; exit 1; }
ceph_ -s | head -20
log "=== 00 complete ==="
