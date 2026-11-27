#!/bin/bash
# After the Ceph arms finish: one engine at a time on the host (docker via ssh),
# deep1b1m then deep1b10m, each loaded -> measured (>=120 s per point) -> dropped,
# then the engine's compose is stopped before the next engine starts.
R=/home/dev_path/experiments/scale-deep
L=$R/chain.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
CEPH_PID=${1:-}
log "=== chain: waiting for Ceph (pid ${CEPH_PID:-none}) ==="
[ -n "$CEPH_PID" ] && while kill -0 $CEPH_PID 2>/dev/null; do sleep 60; done
log "ceph run_all ended: $(tail -1 $R/ceph/logs/run_all.log)"
grep -q "ALL DONE" $R/ceph/logs/run_all.log || log "WARN: ceph did not report ALL DONE; engines run anyway"
# Ceph OSDs off: the engines use the same cores 0-7
for p in $(pgrep -x crimson-osd); do
  grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && { kill $p; log "stopped ceph osd $p"; }
done
sleep 30
pgrep -x crimson-osd >/dev/null && { kill -9 $(pgrep -x crimson-osd); log "SIGKILL leftover crimson-osd"; }
ready() {  # $1 engine
  case $1 in
    milvus) curl -sf --max-time 5 localhost:9091/healthz >/dev/null ;;
    qdrant) curl -sf --max-time 5 localhost:6333/collections >/dev/null ;;
    vespa)  curl -sf --max-time 5 localhost:19071/state/v1/health 2>/dev/null | grep -q '"up"' ;;
  esac; }
for ENG in milvus qdrant vespa; do
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
