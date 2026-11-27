#!/bin/bash
# Waits for the running Milvus default load to finish, then replaces the old chain
# (full C ladder + 3 reps) by run_mem48_resume2.sh (FAST_PROTO). If the load never
# finishes (run_engine exits / guard kills it), does nothing: the old chain goes on into
# the mmap fallback, whose run_engine.sh already reads FAST_PROTO.
D=/home/dev_path/experiments/scale-deep/mem48; L=$D/chain.log
LG=/home/dev_path/experiments/scale-deep/engines/logs/milvus_deep1b100m_mem48_default.log
CHAIN=$1; RE=$2
killtree() { for c in $(ps -o pid= --ppid $1); do killtree $c; done; kill $1 2>/dev/null; }
while kill -0 $RE 2>/dev/null; do
  if grep -q "load done in" $LG; then
    kill $CHAIN; killtree $RE; sleep 5
    echo "[$(date -u +%FT%TZ)] milvus[default] load done -> old chain stopped before the C ladder; FAST_PROTO resume2 starts after 5 min drain" | tee -a $L >> $LG
    sleep 300
    cd $D && nohup setsid bash $D/run_mem48_resume2.sh >> $D/resume2.out 2>&1 < /dev/null &
    exit 0
  fi
  sleep 10
done
echo "[$(date -u +%FT%TZ)] fast_switch: milvus default run_engine ended without 'load done' -> old chain continues (mmap path uses FAST_PROTO)" >> $L
