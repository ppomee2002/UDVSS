#!/bin/bash
# Vespa 100M (user 2026-10-13 "큐드란트 끝나면 베스파 100M 바로 해줘"). Milvus volumes wiped
# beforehand (user-approved). Same compose (cpuset 12-19) and protocol as Vespa 1M/10M;
# streamed feed, 64 feed threads (feed is not a measured phase), 48 h settle timeout.
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
H "docker ps --format '{{.Names}}'" | grep -qE 'milvus|qb-qdrant' && { log "FATAL vespa100m: another engine is up"; exit 1; }
FREE=$(H "df -P /home/ubuntu | awk 'NR==2{print int(\$4/1048576)}'"); log "host free ${FREE}G"
[ "${FREE:-0}" -ge 150 ] || { log "FATAL vespa100m: only ${FREE}G free (< 150G)"; exit 1; }
ready() { curl -sf --max-time 5 localhost:19071/state/v1/health 2>/dev/null | grep -q '"up"' &&
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 localhost:8080/ApplicationStatus)" != 000 ]; }
log "--- vespa100m: compose up ---"
H "cd /home/ubuntu/vespa-bench && docker compose up -d" >> $L 2>&1
for i in $(seq 1 90); do ready && break; sleep 10; done
ready || { log "FATAL vespa100m: not ready after 15 min"; H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1; exit 1; }
sleep 30; log "vespa100m ready (19071 + 8080)"
VB_FEED_THREADS=64 VB_INDEX_TIMEOUT=172800 bash $R/engines/run_engine.sh vespa deep1b100m >> $R/engines/logs/chain_vespa.out 2>&1 \
  && log "vespa deep1b100m done" || log "FAILED: vespa deep1b100m (see engines/logs/vespa_deep1b100m.log)"
H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1
log "vespa stopped (index kept)"; log "=== VESPA100M DONE ==="
