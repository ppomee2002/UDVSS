#!/bin/bash
# Exits (=> notification) on completion or the first sign of trouble in the
# (e)(f) run: Milvus "!!" lines (scale/balance failures, UNSTABLE CV, bench
# errors -- the harmless mixcoord idle-baseline note excluded), Qdrant
# Traceback/FAILED, sqnexp pods Pending/CrashLoop/Error, or 10 min without
# progress in either log.
L=/home/dev_path/experiments/ceph-s-qn-scaling/qv/results_unlimited/run_e.log
H="ssh -o BatchMode=yes -o ConnectTimeout=10 -p 31234 ubuntu@k8s-gw"
while sleep 60; do
  grep -q "e done" $L && { echo "DONE"; break; }
  f=$(grep -E "FAILED|Traceback" $L | tail -1); [ -n "$f" ] && { echo "RUN: $f"; break; }
  ML=$($H 'ls -t ~/milvus-motivation/results/m2_sxq_diag_*.log | head -1')
  m=$($H "grep -E '!!' $ML | grep -v 'idle-baseline underflow on .*mixcoord' | tail -1")
  [ -n "$m" ] && { echo "MILVUS: $m"; break; }
  p=$($H 'KUBECONFIG=/home/ubuntu/.kube/config kubectl get pods -n sqnexp --no-headers 2>/dev/null' | grep -E "Pending|CrashLoop|Error|OOMKilled" | head -2)
  [ -n "$p" ] && { sleep 60; p=$($H 'KUBECONFIG=/home/ubuntu/.kube/config kubectl get pods -n sqnexp --no-headers 2>/dev/null' | grep -E "Pending|CrashLoop|Error|OOMKilled" | head -2); [ -n "$p" ] && { echo "PODS: $p"; break; }; }
  ml=$($H "stat -c %Y $ML"); now=$(date +%s)
  idle=$(( now - $(stat -c %Y $L) )); midle=$(( now - ml ))
  [ $idle -gt 600 ] && [ $midle -gt 600 ] && { echo "STALL: run ${idle}s, milvus ${midle}s"; break; }
done
date -u; tail -6 $L
