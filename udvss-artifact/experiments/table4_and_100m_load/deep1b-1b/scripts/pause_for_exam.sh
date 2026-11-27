#!/bin/bash
# 2026-10-27: course coding test on 94 from 15:30 KST -> pause the Vespa 1B feed at
# 06:15Z (SIGSTOP feeder processes, Vespa cpuset back to 12-19). Resume with resume_after_exam.sh.
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
until [ "$(date -u +%H%M)" -ge 0615 ]; do sleep 20; done
P=$(ps -eo pid,args | awk '$0 ~ /vespa_feed_mp.py deep1b1b/ && $0 !~ /awk/ {print $1}' | tr '\n' ' ')
kill -STOP $P
H "docker update --cpuset-cpus 12-19 vb-vespa"
log "exam: Vespa feed PAUSED (SIGSTOP $P; cpuset 12-19) for the coding test; fed so far: $(grep -aoE 'fed +[0-9]+' $X/logs/vespa_1b.log | tail -1)"
