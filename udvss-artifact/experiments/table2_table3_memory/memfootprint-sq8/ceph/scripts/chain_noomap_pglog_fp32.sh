#!/bin/bash
# 2026-10-17 user: the same 4 GiB Ceph capacity sweep as chain_noomap_pglog.sh (int8), now FP32
# (no SQ8). Identical settings: NO_OMAP identity, osd_pg_log_dups_tracked=100 +
# osd_min_pg_log_entries=50 in ceph.conf from creation, smp 2, RBM + cbjournal 1.2G, cachepin
# 500 MiB, crimson_memory 4G, PG 1024, M16, NQ 200, one OSD process (no restart), OSD cores 8-9,
# client 16-23,40-47. Starts only after the Vespa mem40 run is done (no CPU/page-cache overlap).
# Stops at the first failing N (STOP_ON_FAIL). Device 24G -> fp32 fits up to ~6M.
cd /home/dev_path/experiments/memfootprint-sq8/ceph
PY=/home/dev_path/experiments/memfootprint-sq8/.venv/bin/python
BIN=/home/dev_path/ceph-vectornode-seastore-batch/build/bin; export LD_LIBRARY_PATH=$BIN/../lib
CONF=/home/dev_path/vstart/memfp2-fp32/ceph.conf; RT=noomap_pglog
log(){ echo "[$(date -u +%FT%TZ)] $*"; }
log "start (user 2026-10-17: Vespa mem40 feed paused with SIGSTOP for the duration of this run)"
export PGLOG_DUPS=100 PGLOG_MIN=50
log "create cluster"; ARM=fp32 CACHEPIN=524288000 CBJ=1288486912 bash scripts/00_cluster_prof.sh | tail -2 || exit 1
grep -E "pg_log|crimson_memory|cachepin" $CONF | sort | uniq -c
mkdir -p results/$RT
MB_OUT=results/$RT/mem_breakdown_fp32.jsonl nohup $PY -u scripts/mem_breakdown.py > logs/mem_breakdown_${RT}_fp32.out 2>&1 & mb=$!
( sleep 900; for k in osd_pg_log_dups_tracked osd_min_pg_log_entries crimson_memory seastore_cachepin_size_pershard; do
    echo "$k=$($BIN/ceph -c $CONF daemon osd.0 config get $k 2>&1 | tr -d '\n ')"; done; \
  tr '\0' '\n' < /proc/$(pgrep -f "crimson-osd -i 0 -c $CONF" | head -1)/environ | grep NO_OMAP ) > results/$RT/verify_config_fp32.txt 2>&1 &
env VARIANT=prof CAPS=4G LOAD_CAP=4G CACHE_FIXED=524288000 CBJ=1288486912 CRIMSON_VECTOR_NO_OMAP_IDENTITY=1 \
    ARM=fp32 RTAG=$RT NO_RESTART=1 OSD_CPUSET_OVERRIDE=8-9 AUG_FILE=$PWD/data/aug_8M.fvecs STOP_ON_FAIL=1 \
    STEPS=250000,500000,1000000,2000000,3000000,4000000,6000000 \
    $PY -u scripts/run_arm.py > logs/${RT}_fp32.out 2>&1
kill $mb
log "=== NOOMAP_PGLOG FP32 DONE ==="
