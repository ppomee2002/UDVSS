#!/bin/bash
# chain4 (2026-10-10 12:55Z). My route-sim overlapped Milvus 10M from 12:21 to 12:51
# (Milvus CPU 6.2-6.6 instead of 7.9), so: re-measure Milvus 10M in full on the
# still-loaded collection; THEN, with nothing being measured, finish the paused
# route-sim and pick the d10m_pg4096 pivot variant; then Ceph d1m, d10m_pg4096,
# Qdrant, Vespa.
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
RUNONE=$1; RS_BENCH=$2; RS_DRIVER=$3
log "=== chain4: waiting for the in-flight milvus run_one (pid $RUNONE) ==="
while kill -0 $RUNONE 2>/dev/null; do sleep 20; done
mkdir -p $R/engines/logs/contaminated && cp $R/engines/logs/milvus_deep1b10m.log $R/engines/logs/contaminated/
log "--- milvus deep1b10m: full clean re-measure (--no-load) ---"
bash $R/engines/run_engine.sh milvus deep1b10m --no-load --drop >> $R/engines/logs/chain_milvus.out 2>&1 \
  && log "milvus deep1b10m done (clean)" || log "FAILED: milvus deep1b10m re-measure"
H "cd /home/ubuntu/milvus-bench && docker compose stop" >> $L 2>&1; log "milvus stopped"
log "--- route-sim (nothing measured meanwhile): resume ---"
kill -CONT $RS_BENCH $RS_DRIVER 2>/dev/null
while kill -0 $RS_DRIVER 2>/dev/null; do sleep 30; done
python3 $R/routesim/pick.py > $R/routesim/pick.txt 2>&1
ROUTING=$(tail -1 $R/routesim/pick.txt)
case "$ROUTING" in --pivot-build-sample*) ;; *) log "FATAL: route-sim pick failed: $(tail -3 $R/routesim/pick.txt)"; ROUTING="--pivot-build-sample,65536,--pivot-refine-iters,3";; esac
awk -F'\t' -v OFS='\t' -v r="$ROUTING" '$1=="d10m_pg4096"{$15=r} {print}' $R/ceph/arms.tsv > $R/ceph/arms.tsv.new && mv $R/ceph/arms.tsv.new $R/ceph/arms.tsv
log "route-sim pick: $(grep best $R/routesim/pick.txt) -> d10m_pg4096 routing $ROUTING"
log "--- ceph d1m ---"
KEEP=1 bash $R/ceph/scripts/run_arm.sh d1m "1 2 4 8 16 32 64 96" 16 >> $R/ceph/logs/run_all.log 2>&1 \
  && log "ceph d1m done" || log "FAILED: ceph d1m"
log "--- ceph d10m_pg4096 (user 2026-10-10: extra arm, PG 4096, sample 65536 = 16/PG as ll3) ---"
KEEP=1 bash $R/ceph/scripts/run_arm.sh d10m_pg4096 "1 2 4 8 16 32 64 96" 16 >> $R/ceph/logs/run_all.log 2>&1 \
  && log "ceph d10m_pg4096 done" || log "FAILED: ceph d10m_pg4096"
for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
sleep 30; pgrep -x crimson-osd >/dev/null && kill -9 $(pgrep -x crimson-osd)
log "ceph osds stopped"
ready() { case $1 in
    qdrant) curl -sf --max-time 5 localhost:6333/collections >/dev/null ;;
    vespa)  curl -sf --max-time 5 localhost:19071/state/v1/health 2>/dev/null | grep -q '"up"' ;;
  esac; }
for ENG in qdrant vespa; do
  log "--- $ENG: compose up ---"
  H "cd /home/ubuntu/$ENG-bench && docker compose up -d" >> $L 2>&1
  for i in $(seq 1 60); do ready $ENG && break; sleep 10; done
  ready $ENG || { log "FATAL: $ENG not ready after 10 min"; H "cd /home/ubuntu/$ENG-bench && docker compose stop" >> $L 2>&1; continue; }
  log "$ENG ready"
  for DS in deep1b1m deep1b10m; do
    bash $R/engines/run_engine.sh $ENG $DS --drop >> $R/engines/logs/chain_${ENG}.out 2>&1 \
      && log "$ENG $DS done" || log "FAILED: $ENG $DS (see engines/logs/${ENG}_${DS}.log)"
  done
  H "cd /home/ubuntu/$ENG-bench && docker compose stop" >> $L 2>&1
  log "$ENG stopped"
done
log "=== CHAIN DONE ==="
