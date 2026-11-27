#!/usr/bin/env bash
# One chain = one (system, representation), phases run in order, each a fresh pod.
#   p1-32g : scaling curve with a generous 32 GiB cgroup limit (no pressure expected)
#   p2-8g  : 8 GiB limit, grow in 0.5M steps until the first memory-pressure failure
#   p3-4g  : 4 GiB limit, grow in 0.25M steps until failure
#   p4-16g : 16 GiB limit, 1M steps up to 10M (only if time permits)
# A failure in one phase never stops the chain; it is the measurement.
set -u
S=$1; M=$2
cd /home/dev_path/experiments/memfootprint-sq8
PY=".venv/bin/python -u scripts/run_series.py"
export OMP_NUM_THREADS=2
run() { echo "[$(date -u +%FT%TZ)] START $S $M $*"; taskset -c 17-23,41-47 $PY --system $S --mode $M "$@"; echo "[$(date -u +%FT%TZ)] END $S $M $* rc=$?"; }
run --mem 32Gi --tag p1-32g --steps 100000,250000,500000,1000000,2000000,4000000
run --mem 8Gi  --tag p2-8g  --steps $(seq -s, 500000 500000 6000000)
run --mem 4Gi  --tag p3-4g  --steps $(seq -s, 250000 250000 3000000)
run --mem 16Gi --tag p4-16g --steps $(seq -s, 1000000 1000000 10000000)
echo "[$(date -u +%FT%TZ)] CHAIN DONE $S $M"
