#!/bin/bash
# chain6: Vespa-only re-run. chain5 started the load as soon as the config server (19071)
# reported up, before the container on 8080 answered -> connection reset. Wait for 8080 too.
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
pgrep -x crimson-osd >/dev/null && { log "FATAL: a crimson-osd is still up"; exit 1; }
ready() { curl -sf --max-time 5 localhost:19071/state/v1/health 2>/dev/null | grep -q '"up"' &&
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 localhost:8080/ApplicationStatus)" != 000 ]; }
log "--- chain6 vespa: compose up ---"
H "cd /home/ubuntu/vespa-bench && docker compose up -d" >> $L 2>&1
for i in $(seq 1 90); do ready && break; sleep 10; done
ready || { log "FATAL: vespa 8080 not ready after 15 min"; H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1; exit 1; }
sleep 30; log "vespa ready (19071 + 8080)"
for DS in deep1b1m deep1b10m; do
  bash $R/engines/run_engine.sh vespa $DS --drop >> $R/engines/logs/chain_vespa.out 2>&1 \
    && log "vespa $DS done" || log "FAILED: vespa $DS (see engines/logs/vespa_${DS}.log)"
done
H "cd /home/ubuntu/vespa-bench && docker compose stop" >> $L 2>&1
log "vespa stopped"; log "=== CHAIN6 DONE ==="
