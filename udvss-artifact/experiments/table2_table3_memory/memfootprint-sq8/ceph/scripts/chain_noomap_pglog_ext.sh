#!/bin/bash
# 2026-10-16 user: if int8 8M passes in noomap_pglog, keep growing N with generated data.
# Fresh cluster (same settings as chain_noomap_pglog.sh; restart after data hits the RBM remount
# assert), steps 10/12/14/16M, stop at the first failing N; if 16M passes and disk allows, 20M, 24M.
cd /home/dev_path/experiments/memfootprint-sq8/ceph
PY=/home/dev_path/experiments/memfootprint-sq8/.venv/bin/python
CONF=/home/dev_path/vstart/memfp2-int8/ceph.conf; RT=noomap_pglog_ext
log(){ echo "[$(date -u +%FT%TZ)] $*"; }
until grep -q "NOOMAP_PGLOG DONE" logs/chain_noomap_pglog.out && grep -q "GEN_EXT DONE" logs/gen_ext.out; do
  grep -q Traceback logs/gen_ext.out && { log "gen_ext failed"; exit 1; }; sleep 60; done
row8=$(grep '"phase": "query", "N": 8000000' logs/noomap_pglog_int8.out | tail -1)
log "8M: $row8"
echo "$row8" | grep -q '"failure": ""' || { log "8M did not pass -> no extension"; log "=== EXT DONE ==="; exit 0; }
own() { ps -eo pid,args | awk -v c="$CONF" 'index($0, c) && !/awk/ {print $1}'; }
teardown() { for p in $(ps -eo pid,args | awk -v c="$CONF" '/ceph-run/ && index($0, c) && !/awk/ {print $1}'); do kill $p; done
  for p in $(own); do kill -TERM $p; done; for i in $(seq 40); do [ -z "$(own)" ] && break; sleep 3; done
  for p in $(own); do kill -KILL $p; done; sleep 2; rm -rf /home/dev_path/vstart/memfp2-int8; }
run() {  # $1 = steps
  teardown; export PGLOG_DUPS=100 PGLOG_MIN=50
  ARM=int8 CACHEPIN=524288000 CBJ=1288486912 bash scripts/00_cluster_prof.sh | tail -2 || return 1
  env VARIANT=prof CAPS=4G LOAD_CAP=4G CACHE_FIXED=524288000 CBJ=1288486912 CRIMSON_VECTOR_NO_OMAP_IDENTITY=1 \
      ARM=int8 RTAG=$RT NO_RESTART=1 STOP_ON_FAIL=1 OSD_CPUSET_OVERRIDE=8-9 AUG_FILE=$PWD/data/aug_ext.fvecs \
      STEPS=$1 $PY -u scripts/run_arm.py >> logs/${RT}_int8.out 2>&1; }
mkdir -p results/$RT
MB_OUT=results/$RT/mem_breakdown.jsonl nohup $PY -u scripts/mem_breakdown.py > logs/mem_breakdown_$RT.out 2>&1 & mb=$!
log "run 10..16M"; run 10000000,12000000,14000000,16000000
if ! grep -q STOP_ON_FAIL logs/${RT}_int8.out && grep '"phase": "query", "N": 16000000' logs/${RT}_int8.out | grep -q '"failure": ""'; then
  free=$(df --output=avail -BG /home/dev_path | tail -1 | tr -dc 0-9)
  if [ $free -ge 40 ]; then
    log "16M passed, free ${free}G -> generating 20M, 24M"
    OMP_NUM_THREADS=6 taskset -c 26-31 $PY -u scripts/gen_ext.py 20000000,24000000 >> logs/gen_ext.out 2>&1
    log "run 20,24M (fresh cluster)"; run 20000000,24000000
  else log "16M passed but only ${free}G free -> stop (needs ~25G for 24M data + headroom)"; fi
fi
kill $mb; log "=== EXT DONE ==="
