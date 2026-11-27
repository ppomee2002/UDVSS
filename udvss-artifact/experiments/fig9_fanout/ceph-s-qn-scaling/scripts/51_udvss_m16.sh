#!/bin/bash
# UDVSS pivot+SQ8 at M=16, otherwise identical to the
# same-CPU run (50_udvss_cpu126.sh): pg_num 1024, 126 CPUs split over the OSDs
# (N=1 capped at 84), client on 94. Rows in results_sq8_cpu126_m16/.
E=/home/dev_path/experiments/ceph-s-qn-scaling
H="ssh -o BatchMode=yes -p 31234 ubuntu@k8s-gw KUBECONFIG=/home/ubuntu/.kube/config kubectl"
$H scale deploy/milvus-querynode -n milvus --replicas=0; sleep 15
export CPU_POOL=all ROUTE=pivot SQ_RES=$E/results_sq8_cpu126_m16
$E/scripts/21_final_sq8.sh
source $E/scripts/env.sh
r109 "pkill -9 -f '[b]undle[-a-z0-9]*/bin/crimson-osd -i' || true"
kill $(exp_pids) 2>/dev/null
r109 "sudo nft delete table inet sqn; sudo sysctl -q -w fs.aio-max-nr=$(cat $E/logs/109_aio_max_nr.orig)"
$H scale deploy/milvus-querynode -n milvus --replicas=1
echo "[$(date -u +%FT%TZ)] === udvss m16 done ==="
