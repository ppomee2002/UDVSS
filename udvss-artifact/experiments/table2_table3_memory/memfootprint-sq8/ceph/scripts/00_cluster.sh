#!/bin/bash
# NEW crimson+seastore cluster for one arm.  Refuses to reuse an existing $VDEST.
source /home/dev_path/experiments/memfootprint-sq8/ceph/scripts/env.sh
set -e
[ -e "$VDEST" ] && { echo "FATAL: $VDEST exists; this script never reuses or removes clusters" >&2; exit 1; }
avail=$(df --output=avail -BG /home/dev_path | tail -1 | tr -dc 0-9)
[ "$avail" -lt 36 ] && { echo "FATAL: only ${avail}G free" >&2; exit 1; }
log "vstart NEW cluster $VDEST (device $DEVICE_SIZE)"
mkdir -p "$VDEST"
cd $BUILD
VSTART_DEST=$VDEST CEPH_CONF=$VDEST/ceph.conf CEPH_PORT=$CEPH_PORT_BASE \
  MON=1 OSD=1 MGR=1 MDS=0 RGW=0 \
  $SRC/src/vstart.sh -n -X --crimson --seastore --crimson-smp 8 \
  --seastore-device-size $DEVICE_SIZE --without-dashboard \
  -o osd_pool_default_size=1 -o osd_pool_default_min_size=1 \
  -o mon_max_pg_per_osd=4000 -o mon_allow_pool_delete=true \
  -o seastore_max_concurrent_transactions=256 -o seastore_segment_size=16M \
  -o seastore_experimental_vector_node=true \
  -o seastore_vector_node_list_block_bytes=$LIST_BLOCK_BYTES \
  > $EXP/logs/00_vstart_$ARM.log 2>&1
python3 - "$VDEST/ceph.conf" "$LIST_BLOCK_BYTES" <<'PY'
import sys
p, nbytes = sys.argv[1], sys.argv[2]
out = []
for l in open(p).read().split('\n'):
    out.append(l)
    if l.strip() in ('[global]', '[osd]', '[client]', '[mon]', '[mgr]'):
        out.append('        seastore_experimental_vector_node=true')
        out.append('        seastore_vector_node_list_block_bytes=%s' % nbytes)
        out.append('        mon_max_pg_per_osd = 4000')
        out.append('        mon_allow_pool_delete = true')
open(p, 'w').write('\n'.join(out))
PY
log "conf patched"; $BIN/ceph -c $CEPH_CONF -s 2>&1 | grep -E "osd:|health" | head -3
