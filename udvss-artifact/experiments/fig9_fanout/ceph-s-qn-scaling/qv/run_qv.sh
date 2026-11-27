#!/bin/bash
# Qdrant (S + QN) -> Vespa (QN) -> Milvus rerun of the S=1 QN points its own
# stability gate flagged (CV > 5%: Q=4/8/16). Milvus QueryNodes stay at 0
# while Qdrant/Vespa own 109; 22_qn_sweep_s.sh scales them itself and
# restores Q=1 at the end.
cd /home/dev_path/experiments/ceph-s-qn-scaling/qv
PY=/home/dev_path/experiments/memfootprint-sq8/.venv/bin/python
log() { echo "[$(date -u +%FT%TZ)] $*"; }
log "=== qdrant ==="; $PY qdrant_sweep.py all || log "FAILED qdrant"
log "=== vespa ===";  $PY vespa_sweep.py || log "FAILED vespa"
log "=== milvus S=1 rerun Q=4/8/16 ==="
ssh -o BatchMode=yes -p 31234 ubuntu@k8s-gw 'cd ~/milvus-motivation; S=1 CHUNKS=16 TAG=M2QNS1C16R QN_LIST="4 8 16" bash scripts/22_qn_sweep_s.sh > results/m2_qnsweep_s1c16_rerun_$(date +%Y%m%d-%H%M%S).log 2>&1' \
  || log "FAILED milvus rerun"
log "=== qv all done ==="
