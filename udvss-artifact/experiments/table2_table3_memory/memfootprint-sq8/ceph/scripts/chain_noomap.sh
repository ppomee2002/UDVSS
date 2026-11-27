#!/bin/bash
# After the OMAP-on reference run (prof12) finishes: same reference settings, per-vector OMAP key OFF.
cd /home/dev_path/experiments/memfootprint-sq8/ceph
PY=/home/dev_path/experiments/memfootprint-sq8/.venv/bin/python
STOP=/tmp/udvss-scratch/stop_memfp2.sh
COMMON="VARIANT=prof CAPS=4G LOAD_CAP=4G CACHE_FIXED=524288000 CBJ=1288486912 CRIMSON_VECTOR_NO_OMAP_IDENTITY=1"
log(){ echo "[$(date -u +%FT%TZ)] $*"; }
until [ $(grep -c "ARM DONE\|Traceback" logs/prof12_fp32.out logs/prof12_int8.out | awk -F: '{s+=$2} END{print s}') -ge 2 ]; do sleep 30; done
log "prof12 finished"; sleep 40
pkill -f "scripts/mem_breakdown.py" ; bash $STOP; sleep 5
fresh(){ bash $STOP >/dev/null; sleep 5; for a in fp32 int8; do ARM=$a CACHEPIN=524288000 CBJ=1288486912 scripts/00_cluster_prof.sh | tail -1; done; }
log "restart test (OMAP off)"; fresh
for a in fp32 int8; do env $COMMON ARM=$a RTAG=noomap_restarttest NO_RESTART=0 STEPS=250000 timeout 2400 $PY -u scripts/run_arm.py > logs/noomap_restart_$a.out 2>&1 & done; wait
for a in fp32 int8; do log "$a restart test: remount-assert=$(grep -ac _remove_from_tree /home/dev_path/vstart/memfp2-$a/out/osd.0.log) bad_alloc=$(grep -ac bad_alloc /home/dev_path/vstart/memfp2-$a/out/osd.0.log)"; mkdir -p logs/noomap_restart_osdlog; grep -a "_remove_from_tree\|Aborting\|bad_alloc" /home/dev_path/vstart/memfp2-$a/out/osd.0.log | head -50 > logs/noomap_restart_osdlog/$a.txt; done
log "full sweep (OMAP off, no restart)"; fresh
MB_OUT=results/noomap/mem_breakdown.jsonl nohup $PY -u scripts/mem_breakdown.py > logs/mem_breakdown_noomap.out 2>&1 &
mkdir -p results/noomap
for a in fp32 int8; do env $COMMON ARM=$a RTAG=noomap NO_RESTART=1 STEPS=250000,500000,1000000,2000000,4000000 $PY -u scripts/run_arm.py > logs/noomap_$a.out 2>&1 & done; wait
pkill -f "scripts/mem_breakdown.py"
log "noomap sweep done"
