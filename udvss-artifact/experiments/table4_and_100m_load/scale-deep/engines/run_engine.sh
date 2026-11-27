#!/bin/bash
# run_engine.sh <milvus|qdrant|vespa> <dataset> [--no-load] [--drop]
# scale-deep sanity/100M protocol for the HNSW engines, same as the Ceph side:
#   load + build (once) -> C ladder 20/40/80/160 at ef=64 (one 120 s run each;
#   C = smallest within 3% of the best QPS) -> ef curve 10..1024, 3 x 120 s each.
# Every run is time-bound (--min-seconds 120) after the harness's warm-up request
# per worker. Engine on cpuset 0-7 (its docker-compose, unchanged); the container
# must already be up -- starting it needs sudo on the host.
set -u
ENG=$1; DS=$2; shift 2
NOLOAD=0; DROP=0
for x in "$@"; do case $x in --no-load) NOLOAD=1;; --drop) DROP=1;; esac; done
R=/home/dev_path/experiments/scale-deep/engines
TAG=${RUN_TAG:+_$RUN_TAG}   # e.g. RUN_TAG=mem48_paged -> separate result dir + log
OUT=$R/results/$ENG/$DS$TAG; mkdir -p $OUT; LOG=$R/logs/${ENG}_${DS}$TAG.log
MINS=${MIN_SECONDS:-120}
EFS="10 16 32 64 128 256 512 1024"
log() { echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a $LOG; }
if [ $ENG = milvus ]; then
  cd /home/dev_path/milvus-bench
  PIN="taskset -c 8-47"           # engine is 0-7; keep the client off it
  ONE() { $PIN .venv/bin/python -u scripts/run_one.py --dataset $DS --ef $1 --concurrency $2 \
          --repeat $3 --min-seconds $MINS --out-dir $OUT; }
  LOAD() { .venv/bin/python -u scripts/load_dataset.py --dataset $DS; }
  DROPF() { .venv/bin/python -u scripts/load_dataset.py --dataset $DS --drop; }
else
  cd /home/dev_path/corun
  PIN="taskset -c 20-23,44-47"    # as in the 1M sweeps
  ONE() { $PIN .venv/bin/python -u scripts/run_one.py --engine $ENG --dataset $DS --ef $1 \
          --concurrency $2 --repeat $3 --min-seconds $MINS --out-dir $OUT --co-run 0; }
  LOAD() { .venv/bin/python -u -c "import sys; sys.path.insert(0,'scripts'); import json, engine_$ENG as E; \
           print(json.dumps(E.load('$DS'), default=str))"; }
  DROPF() { .venv/bin/python -u -c "import sys; sys.path.insert(0,'scripts'); import engine_$ENG as E; E.drop('$DS')"; }
fi
if [ $NOLOAD = 0 ]; then
  log "load $ENG $DS"; t0=$(date +%s)
  LOAD >> $LOG 2>&1 || { log "FATAL: load failed"; exit 1; }
  log "load done in $(( $(date +%s) - t0 ))s"
fi
declare -A Q
# mem48 (user 2026-10-15): while mem48/FAST_PROTO exists, the C ladder stops at the first C
# that FAILED or did not beat the best smaller C by 3% (the selection rule below could not pick
# it), and the ef curve runs 1 rep instead of 3. The 120 s measurement itself is unchanged.
FAST=0; [ -e $R/../mem48/FAST_PROTO ] && FAST=1
REPS="1 2 3"; [ $FAST = 1 ] && { REPS="1"; log "FAST_PROTO: C ladder early stop, 1 rep per ef"; }
best=0
for C in 20 40 80 160; do
  out=$(ONE 64 $C tune 2>&1 | tee -a $LOG | grep -E " qps=" | tail -1); log "  tune C=$C: $out"
  q=$(echo "$out" | grep -oE "qps=[0-9.]+" | cut -d= -f2); [ -n "$q" ] && Q[$C]=$q
  if [ $FAST = 1 ] && { [ -z "$q" ] || echo "$out" | grep -q FAILED || awk -v a=$q -v m=$best 'BEGIN{exit !(a<1.03*m)}'; }; then
    log "  C ladder early stop at C=$C (failed or < 1.03 x best $best)"; break; fi
  [ -n "$q" ] && awk -v a=$q -v m=$best 'BEGIN{exit !(a>m)}' && best=$q
done
max=0; for C in "${!Q[@]}"; do awk -v a=${Q[$C]} -v b=$max 'BEGIN{exit !(a>b)}' && max=${Q[$C]}; done
BC=""; for C in 20 40 80 160; do [ -n "${Q[$C]:-}" ] || continue
  awk -v a=${Q[$C]} -v m=$max 'BEGIN{exit !(a>=0.97*m)}' && { BC=$C; break; }; done
[ -n "$BC" ] || { log "FATAL: C ladder gave nothing"; exit 1; }
log "chosen C=$BC (max qps $max)"; echo -e "$ENG\t$DS$TAG\t$BC\t$max" >> $R/results/chosen_c.tsv
for EF in $EFS; do for REP in $REPS; do
  out=$(ONE $EF $BC r$REP 2>&1 | tee -a $LOG | grep -E " qps=" | tail -1); log "  ef=$EF r$REP: $out"
done; done
[ $DROP = 1 ] && { log "drop $ENG $DS"; DROPF >> $LOG 2>&1; }
log "DONE $ENG $DS"
