#!/bin/bash
# Recovery 2026-10-08: the 1m tree M=384 rep3 row was measured while a duplicate run
# was going (invalid, moved to runs/invalid). The 1m pool had been dropped by the
# next scale's load, so: reload 1m, re-measure that one row, then resume 20_all.
D=/home/dev_path/experiments/paper-100m/ws/scripts
$D/10_load.sh 1m || exit 1
$D/11_query.sh 1m tree 384 3 1 || exit 1
exec $D/20_all.sh
