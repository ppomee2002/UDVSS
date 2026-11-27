#!/bin/bash
# 2026-10-11: PG x4 (16384; bench needs a power of 2, 12288 rejected), d10m, M=16 only, same balanced pivot (64 samples/PG).
R=/home/dev_path/experiments/scale-deep; L=$R/chain.log
log() { echo "[$(date -u +%FT%TZ)] $*" >> $L; }
log "--- ceph d10m_pg16384 (M=16 only) ---"
KEEP=1 bash $R/ceph/scripts/run_arm.sh d10m_pg16384 "16" 16 >> $R/ceph/logs/run_all.log 2>&1 \
  && log "ceph d10m_pg16384 done" || log "FAILED: ceph d10m_pg16384"
for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill $p; done
sleep 30; for p in $(pgrep -x crimson-osd); do grep -qa "/vstart-extra/sd-" /proc/$p/cmdline && kill -9 $p; done
log "=== PG16384 DONE (OSDs stopped) ==="
