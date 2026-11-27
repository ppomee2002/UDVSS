#!/bin/bash
# scale-deep Ceph sanity run (10M first, then the 1M Deep1B-prefix baseline).
# Clusters are kept after measuring (KEEP=1) so nothing has to be reloaded.
S=/home/dev_path/experiments/scale-deep/ceph/scripts
ML="1 2 4 8 16 32 64 96"
KEEP=1 bash $S/run_arm.sh d10m "$ML" 16 || exit 1
KEEP=1 bash $S/run_arm.sh d1m  "$ML" 16 || exit 1
echo "ALL DONE"
