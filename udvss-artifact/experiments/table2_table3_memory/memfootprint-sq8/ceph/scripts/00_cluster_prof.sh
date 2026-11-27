#!/bin/bash
# Reference configuration (2026-10-02): smp 2, RANDOM_BLOCK_SSD main device with a
# 1.5 GiB circular bounded journal, cachepin 1.5 GiB per reactor, crimson_memory 4 GiB.
# NEW clusters /home/dev_path/vstart/memfp2-$ARM; never reuses or removes anything.
set -e
: "${ARM:?ARM=fp32|int8}"
SRC=/home/dev_path/ceph-vectornode-seastore-batch; BUILD=$SRC/build
VDEST=/home/dev_path/vstart/memfp2-$ARM
EXP=/home/dev_path/experiments/memfootprint-sq8/ceph
[ "$ARM" = fp32 ] && PORT=8601 || PORT=8701
B15=1610612736; M4=4294967296
CACHEPIN=${CACHEPIN:-1610612736}   # seastore_cachepin_size_pershard (bytes)
CBJ=${CBJ:-1610612736}             # seastore_cbjournal_size (bytes)
[ -e "$VDEST" ] && { echo "FATAL: $VDEST exists" >&2; exit 1; }
avail=$(df --output=avail -BG /home/dev_path | tail -1 | tr -dc 0-9)
[ "$avail" -lt 34 ] && { echo "FATAL: only ${avail}G free" >&2; exit 1; }
mkdir -p "$VDEST"; cd $BUILD
VSTART_DEST=$VDEST CEPH_CONF=$VDEST/ceph.conf CEPH_PORT=$PORT \
  MON=1 OSD=1 MGR=1 MDS=0 RGW=0 \
  $SRC/src/vstart.sh -n -X --crimson --seastore --crimson-smp 2 \
  --seastore-device-size 24G --without-dashboard \
  -o osd_pool_default_size=1 -o osd_pool_default_min_size=1 \
  -o mon_max_pg_per_osd=4000 -o mon_allow_pool_delete=true \
  -o seastore_max_concurrent_transactions=256 \
  -o seastore_main_device_type=RANDOM_BLOCK_SSD -o seastore_cbjournal_size=$CBJ \
  -o seastore_cachepin_size_pershard=$CACHEPIN -o crimson_memory=$M4 \
  -o seastore_experimental_vector_node=true \
  -o seastore_vector_node_list_block_bytes=16384 \
  > $EXP/logs/00_vstart_prof_$ARM.log 2>&1
python3 - "$VDEST/ceph.conf" "$CACHEPIN" "$CBJ" <<'PY'
import os, sys
p = sys.argv[1]; cp = sys.argv[2]; cbj = sys.argv[3]; out = []
for l in open(p).read().split('\n'):
    out.append(l)
    if l.strip() in ('[global]', '[osd]', '[client]', '[mon]', '[mgr]'):
        out += ['        seastore_experimental_vector_node=true',
                '        seastore_vector_node_list_block_bytes=16384',
                '        seastore_main_device_type=RANDOM_BLOCK_SSD',
                '        seastore_cbjournal_size=%s' % cbj,
                '        seastore_cachepin_size_pershard=%s' % cp,
                '        crimson_memory=4294967296',
                ] + (['        osd_pg_log_dups_tracked=%s' % os.environ['PGLOG_DUPS'],
                      '        osd_min_pg_log_entries=%s' % os.environ['PGLOG_MIN']] if os.environ.get('PGLOG_DUPS') else []) + [
                '        mon_max_pg_per_osd = 4000', '        mon_allow_pool_delete = true']
open(p, 'w').write('\n'.join(out))
PY
echo "[$ARM] cluster $VDEST created"; LD_LIBRARY_PATH=$BUILD/lib $BUILD/bin/ceph -c $VDEST/ceph.conf -s 2>&1 | grep -E "osd:|health"
