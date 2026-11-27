#!/bin/bash
# (e)(f), measuring only what cannot be reused: Milvus S=QN=4 (CHUNKS=16;
# 1x1 = new (c) QN=1, 16x16 = M2QNC16 S16/Q16) and Qdrant shards=peers=1/4
# (unlimited, 1 segment/shard; 16x16 = (c) peers=16). Milvus QueryNodes go to 0 while Qdrant owns
# 109 and back to 1 afterwards.
cd /home/dev_path/experiments/ceph-s-qn-scaling/qv
log() { echo "[$(date -u +%FT%TZ)] $*"; }
H="ssh -o BatchMode=yes -p 31234 ubuntu@k8s-gw"
log "=== milvus S=QN diagonal ==="
$H 'cd ~/milvus-motivation; N_LIST=4 bash scripts/23_sxq_diag.sh > results/m2_sxq_diag_$(date +%Y%m%d-%H%M%S).log 2>&1' || log "FAILED milvus"
$H 'KUBECONFIG=/home/ubuntu/.kube/config kubectl scale deploy/milvus-querynode -n milvus --replicas=0'
sleep 20
log "=== qdrant E (unlimited) ==="
QV_CPU=unlimited /home/dev_path/experiments/memfootprint-sq8/.venv/bin/python qdrant_sweep.py E || log "FAILED qdrant"
$H 'KUBECONFIG=/home/ubuntu/.kube/config kubectl scale deploy/milvus-querynode -n milvus --replicas=1'
log "=== e done ==="
