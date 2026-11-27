#!/bin/bash
# Same-CPU-condition rerun: Qdrant and Vespa with no CPU/memory limits (as the
# Milvus QueryNodes), segment counts still pinned for Qdrant. Rows go to
# results_unlimited/; the 4-CPU results in results/ are left untouched.
cd /home/dev_path/experiments/ceph-s-qn-scaling/qv
PY=/home/dev_path/experiments/memfootprint-sq8/.venv/bin/python
export QV_CPU=unlimited
mkdir -p results_unlimited
log() { echo "[$(date -u +%FT%TZ)] $*"; }
log "=== qdrant (unlimited) ==="; $PY qdrant_sweep.py all || log "FAILED qdrant"
log "=== vespa (unlimited) ===";  $PY vespa_sweep.py || log "FAILED vespa"
log "=== unlimited done ==="
