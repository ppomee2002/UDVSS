#!/bin/bash
# User 2026-10-10 15:10Z: pause after the Ceph arms (d1m, d10m_pg4096); Qdrant/Vespa on hold.
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log; CH=$1
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
until grep -qE "ceph d10m_pg4096 done|FAILED: ceph d10m_pg4096" $L; do
  kill -0 $CH 2>/dev/null || { log "pause gate: chain4 ended on its own"; exit 0; }
  sleep 5
done
kill -9 $CH; log "PAUSED (user): chain4 stopped after the Ceph arms; Qdrant/Vespa not started"
for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
sleep 30; for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill -9 $p; done
log "ceph osds stopped; idle"
