#!/bin/bash
# Milvus Deep1B 1B on 110 (user 2026-10-28). Chain:
#  0. wait for base.1B.fbin copy (384000000008 B)
#  1. default: parallel insert (24 procs, client cpuset 0-47) -> flush -> HNSW M30/efc360 index
#     (standalone on NUMA-1 104 threads) -> load_collection (default = in memory)
#  2. if the default load fails: restart standalone with the mmap override (vectorField +
#     vectorIndex mmap, as mem48) and load again (no re-insert / re-index)
#  3. if loaded: standalone cpuset -> 52-59 (8 cores as 0-7 on 94), measure as run_engine.sh
#     FAST_PROTO (C 20/40/80/160 at ef64 early stop, ef 10..1024 x1, 120 s), client 60-79,164-183
# Disk watcher: volume avail < 2 GiB for 30 min -> record "disk full" and stop.
# Marker "=== MILVUS 1B DONE ===".
W=$HOME/milvus1b; L=$W/logs/driver.log; mkdir -p $W/logs
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a $L; }
CL=$W/client.sh; DS=deep1b1b
done_() { touch $W/logs/WATCH_STOP; log "=== MILVUS 1B DONE ==="; exit ${1:-0}; }
avail() { docker run --rm -v milvus1b:/v:ro python:3.12-slim df -B1G --output=avail /v | tail -1 | tr -d ' '; }
log "0: waiting for the base copy"
until [ "$(stat -c %s $W/datasets/$DS/base.1B.fbin 2>/dev/null)" = 384000000008 ] && ! pgrep -u $USER -f "scp.*base.1B.fbin" >/dev/null; do sleep 60; done
log "0: base copy complete"
( low=0; while [ ! -e $W/logs/WATCH_STOP ]; do
    a=$(avail); m=$(docker stats --no-stream --format '{{.Name}}={{.MemUsage}}' m1b-standalone m1b-minio m1b-etcd | tr '\n' ' ')
    echo -e "$(date -u +%FT%TZ)\tavail_g=$a\t$m" >> $W/logs/disk_mem.tsv
    [ "${a:-99}" -lt 2 ] && low=$((low+1)) || low=0
    if [ $low -ge 6 ]; then
      echo "[$(date -u +%FT%TZ)] WATCH: volume avail ${a}G < 2G for 30 min -> DISK FULL; stopping the loader" | tee -a $L
      docker ps -q --filter ancestor=mvb-client:1 | xargs -r docker kill; touch $W/logs/DISK_FULL; break
    fi
    sleep 300; done ) &
log "1: default: insert + index + load (24 insert procs)"
MVB_CPUSET=52-103,156-207 $CL 0-47 python -u scripts/load_parallel.py --dataset $DS --procs 24 >> $W/logs/load_default.log 2>&1
RC=$?
[ -e $W/logs/DISK_FULL ] && { log "RESULT: Milvus 1B cannot load within the 916G disk (disk full; see load_default.log, disk_mem.tsv)"; done_ 1; }
if [ $RC != 0 ]; then
  grep -q "index build" $W/logs/load_default.log || { log "RESULT: insert/index FAILED (rc=$RC): $(grep -aE 'Error|error|Exception' $W/logs/load_default.log | tail -1 | cut -c1-300)"; done_ 1; }
  log "1: RESULT default load FAILED: $(grep -aE 'Error|error|Exception|resource' $W/logs/load_default.log | tail -1 | cut -c1-300)"
  log "2: mmap: restart standalone with vectorField+vectorIndex mmap, load again"
  cd $W && docker compose -f docker-compose.yml -f docker-compose.mmap.yml up -d standalone >> $L 2>&1
  for i in $(seq 120); do [ "$(docker inspect -f '{{.State.Health.Status}}' m1b-standalone)" = healthy ] && break; sleep 10; done
  sleep 120
  MVB_CPUSET=52-103,156-207 $CL 0-47 python -u scripts/load_parallel.py --dataset $DS --load-only >> $W/logs/load_mmap.log 2>&1 \
    || { log "RESULT: mmap load FAILED too: $(grep -aE 'Error|error|Exception|resource' $W/logs/load_mmap.log | tail -1 | cut -c1-300)"; done_ 1; }
  MODE=mmap
else MODE=default; fi
log "2: loaded in $MODE mode ($(tail -1 $W/logs/load_*.log | cut -c1-200)); measuring on cpuset 52-59"
docker update --cpuset-cpus 52-59 m1b-standalone >> $L 2>&1; sleep 60
OUT=results/$DS_$MODE; OUT=results/${DS}_$MODE
ONE() { MVB_CPUSET=52-59 $CL 60-79,164-183 python -u scripts/run_one.py --dataset $DS --ef $1 --concurrency $2 \
        --repeat $3 --min-seconds 120 --out-dir $OUT; }
declare -A Q; best=0
for C in 20 40 80 160; do
  out=$(ONE 64 $C tune 2>&1 | tee -a $W/logs/measure.log | grep -E " qps=" | tail -1); log "  tune C=$C: $out"
  q=$(echo "$out" | grep -oE "qps=[0-9.]+" | cut -d= -f2); [ -n "$q" ] && Q[$C]=$q
  if [ -z "$q" ] || echo "$out" | grep -q FAILED || awk -v a=$q -v m=$best 'BEGIN{exit !(a<1.03*m)}'; then
    log "  C ladder early stop at C=$C"; break; fi
  awk -v a=$q -v m=$best 'BEGIN{exit !(a>m)}' && best=$q
done
max=0; for C in "${!Q[@]}"; do awk -v a=${Q[$C]} -v b=$max 'BEGIN{exit !(a>b)}' && max=${Q[$C]}; done
BC=""; for C in 20 40 80 160; do [ -n "${Q[$C]:-}" ] || continue
  awk -v a=${Q[$C]} -v m=$max 'BEGIN{exit !(a>=0.97*m)}' && { BC=$C; break; }; done
[ -n "$BC" ] || { log "FATAL: C ladder gave nothing"; done_ 1; }
log "chosen C=$BC (max qps $max)"
for EF in 10 16 32 64 128 256 512 1024; do
  out=$(ONE $EF $BC r1 2>&1 | tee -a $W/logs/measure.log | grep -E " qps=" | tail -1); log "  ef=$EF r1: $out"
done
done_ 0
