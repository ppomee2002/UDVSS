#!/bin/bash
# d1m failed at create (d10m's OSD was still up); run it after the engine chain.
CH=$1; S=/home/dev_path/experiments/scale-deep/ceph/scripts
while kill -0 $CH 2>/dev/null; do sleep 60; done
KEEP=1 bash $S/run_arm.sh d1m "1 2 4 8 16 32 64 96" 16 && echo "ALL DONE (d1m after chain)"
