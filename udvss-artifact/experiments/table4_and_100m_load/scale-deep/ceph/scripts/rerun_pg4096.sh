#!/bin/bash
# Re-run of the d10m_pg4096 arm after the 16:01Z pool-create failure; then stop (user: pause after Ceph).
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
log "--- ceph d10m_pg4096 re-run (measure only; load accepted) ---"
KEEP=1 bash $R/ceph/scripts/run_arm.sh d10m_pg4096 "1 2 4 8 16 32 64 96" 16 >> $R/ceph/logs/run_all.log 2>&1 \
  && log "ceph d10m_pg4096 done" || log "FAILED: ceph d10m_pg4096 (re-run)"
for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
sleep 30; for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill -9 $p; done
log "PAUSED (user): Ceph arms done, OSDs stopped; Qdrant/Vespa on hold"
