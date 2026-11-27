#!/bin/bash
# 2026-10-16 user: re-run the Ceph 4 GiB capacity sweep, SQ8 (int8) only, with the mem48 settings:
# CRIMSON_VECTOR_NO_OMAP_IDENTITY=1 + osd_pg_log_dups_tracked=100 + osd_min_pg_log_entries=50
# (here set in ceph.conf from cluster creation, not at runtime). Everything else = the "noomap"/
# "noomap_ext" reference config (smp 2, RBM + cbjournal 1.2G, cachepin 500 MiB, crimson_memory 4G,
# PG 1024, M16, NQ 200, one OSD process, no restart between N). OSD on cores 8-9 (0-7 = mem48 OSD).
cd /home/dev_path/experiments/memfootprint-sq8/ceph
PY=/home/dev_path/experiments/memfootprint-sq8/.venv/bin/python
BIN=/home/dev_path/ceph-vectornode-seastore-batch/build/bin; export LD_LIBRARY_PATH=$BIN/../lib
CONF=/home/dev_path/vstart/memfp2-int8/ceph.conf; RT=noomap_pglog
log(){ echo "[$(date -u +%FT%TZ)] $*"; }
export PGLOG_DUPS=100 PGLOG_MIN=50
log "create cluster"; ARM=int8 CACHEPIN=524288000 CBJ=1288486912 bash scripts/00_cluster_prof.sh | tail -2 || exit 1
grep -E "pg_log|crimson_memory|cachepin" $CONF | sort | uniq -c
mkdir -p results/$RT
MB_OUT=results/$RT/mem_breakdown.jsonl nohup $PY -u scripts/mem_breakdown.py > logs/mem_breakdown_$RT.out 2>&1 & mb=$!
( sleep 900; for k in osd_pg_log_dups_tracked osd_min_pg_log_entries crimson_memory seastore_cachepin_size_pershard; do
    echo "$k=$($BIN/ceph -c $CONF daemon osd.0 config get $k 2>&1 | tr -d '\n ')"; done; \
  tr '\0' '\n' < /proc/$(pgrep -f "crimson-osd -i 0 -c $CONF" | head -1)/environ | grep NO_OMAP ) > results/$RT/verify_config.txt 2>&1 &
env VARIANT=prof CAPS=4G LOAD_CAP=4G CACHE_FIXED=524288000 CBJ=1288486912 CRIMSON_VECTOR_NO_OMAP_IDENTITY=1 \
    ARM=int8 RTAG=$RT NO_RESTART=1 OSD_CPUSET_OVERRIDE=8-9 AUG_FILE=$PWD/data/aug_8M.fvecs \
    STEPS=250000,500000,1000000,2000000,4000000,6000000,8000000 \
    $PY -u scripts/run_arm.py > logs/${RT}_int8.out 2>&1
kill $mb
log "=== NOOMAP_PGLOG DONE ==="
