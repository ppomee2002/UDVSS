#!/bin/bash
# UDVSS pivot+SQ8 (pg 1024, M 32) on the same total CPU budget as the
# unlimited Milvus QueryNodes: 109's 126 free logical CPUs split across the N
# OSDs (CPU_POOL=all; N=1 capped at 84 by SeaStore's superblock). Rows in
# results_sq8_cpu126/; the 4-CPU/OSD results in results_sq8/ stay as they are.
# Waits for the unlimited Qdrant/Vespa run, then restores 109 and Milvus.
E=/home/dev_path/experiments/ceph-s-qn-scaling
until grep -q "unlimited done" $E/qv/results_unlimited/run.log 2>/dev/null; do sleep 60; done
export CPU_POOL=all ROUTE=pivot SQ_RES=$E/results_sq8_cpu126
$E/scripts/21_final_sq8.sh
source $E/scripts/env.sh
r109 "pkill -9 -f '[b]undle[-a-z0-9]*/bin/crimson-osd -i' || true"
kill $(exp_pids) 2>/dev/null
r109 "sudo nft delete table inet sqn; sudo sysctl -q -w fs.aio-max-nr=$(cat $E/logs/109_aio_max_nr.orig)"
ssh -o BatchMode=yes -p 31234 ubuntu@k8s-gw 'KUBECONFIG=/home/ubuntu/.kube/config kubectl scale deploy/milvus-querynode -n milvus --replicas=1'
echo "[$(date -u +%FT%TZ)] === udvss cpu126 done (OSD device files on 109 left for manual delete) ==="
