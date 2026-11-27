#!/bin/bash
# Milvus 100M (user 2026-10-12 "이거 끝나고 milvus 100m진행").
# 1. wait for the Ceph 100M sweep (never share CPU/IO with a measurement)
# 2. disk: delete the 10M Ceph clusters (user-approved), archive+truncate the 100M OSD log
# 3. Milvus up -> run_engine milvus deep1b100m (load streams the base in chunks) -> stop
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
until grep -q "=== D100M DONE" $R/d100m/driver.log; do sleep 60; done
pgrep -x crimson-osd >/dev/null && { log "FATAL milvus100m: a crimson-osd is still up"; exit 1; }
log "--- milvus100m: freeing disk ---"
for a in d10m d10m_pg4096; do bash $R/ceph/scripts/teardown.sh $a >> $L 2>&1 || log "teardown $a failed"; done
OL=/vstart-extra/sd-d100m-pg16384/out/osd.0.log; mkdir -p $R/ceph/logs/clusters/d100m_pg16384
tail -c 500000000 $OL > $R/ceph/logs/clusters/d100m_pg16384/osd.0.log.tail 2>/dev/null && : > $OL
FREE=$(H "df -P /home/ubuntu | awk 'NR==2{print int(\$4/1048576)}'"); log "host free ${FREE}G"
[ "${FREE:-0}" -ge 150 ] || { log "FATAL milvus100m: only ${FREE}G free (< 150G)"; exit 1; }
log "--- milvus100m: compose up ---"
H "cd /home/ubuntu/milvus-bench && docker compose up -d" >> $L 2>&1
for i in $(seq 1 60); do curl -sf --max-time 5 localhost:9091/healthz >/dev/null && break; sleep 10; done
curl -sf --max-time 5 localhost:9091/healthz >/dev/null || { log "FATAL milvus100m: not healthy"; exit 1; }
sleep 30; log "milvus healthy"
bash $R/engines/run_engine.sh milvus deep1b100m >> $R/engines/logs/chain_milvus.out 2>&1 \
  && log "milvus deep1b100m done" || log "FAILED: milvus deep1b100m (see engines/logs/milvus_deep1b100m.log)"
H "cd /home/ubuntu/milvus-bench && docker compose stop" >> $L 2>&1
log "milvus stopped (collection kept)"; log "=== MILVUS100M DONE ==="
