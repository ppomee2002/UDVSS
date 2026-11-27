#!/bin/bash
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
H "docker update --cpuset-cpus 0-15,24-39 vb-vespa"
P=$(ps -eo pid,args | awk '$0 ~ /vespa_feed_mp.py deep1b1b/ && $0 !~ /awk/ {print $1}' | tr '\n' ' ')
kill -CONT $P
log "exam: Vespa feed RESUMED (SIGCONT $P; cpuset 0-15,24-39)"
