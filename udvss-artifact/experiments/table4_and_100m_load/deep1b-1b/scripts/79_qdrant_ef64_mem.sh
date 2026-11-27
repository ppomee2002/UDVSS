#!/bin/bash
# Qdrant 1B ef64 C=1 re-run WITH memory sampling (user 2026-10-29): same call as 78
# (client taskset 20-23,44-47, 120 s time-bound, QB_TIMEOUT 7200). Sampler every 15 s:
# cgroup memory.current/anon/file/oom_kill + nvme read IOPS -> results/qdrant_1b_ef64_mem.tsv
source /home/dev_path/experiments/deep1b-1b/scripts/env.sh
R=/home/dev_path/experiments/scale-deep/engines
OUT=$R/results/qdrant/deep1b1b_c1_mem; mkdir -p $OUT; LOG=$R/logs/qdrant_deep1b1b_c1_mem.log
TSV=$X/results/qdrant_1b_ef64_mem.tsv
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@" < /dev/null; }
CG=/sys/fs/cgroup/system.slice/docker-$(H "docker inspect -f '{{.Id}}' qb-qdrant").scope
echo -e "utc\tphase\tcurrent_g\tanon_g\tfile_g\tmax_g\toom_kill\tread_iops\tread_mb_s" > $TSV
sampler() {
  while [ ! -e $X/logs/79.stop ]; do
    H "a=\$(awk '\$3==\"nvme0n1\"{print \$4,\$6}' /proc/diskstats); sleep 5; b=\$(awk '\$3==\"nvme0n1\"{print \$4,\$6}' /proc/diskstats);
       awk '\$1==\"anon\"{a=\$2} \$1==\"file\"{f=\$2} END{printf \"%.2f\t%.2f\t\", a/2^30, f/2^30}' $CG/memory.stat | tr -d '\n' > /tmp/m;
       printf '%.2f\t%s' \$(echo \"\$(cat $CG/memory.current)/1073741824\" | bc -l) \"\$(cat /tmp/m)\"; printf '%.2f\t' \$(echo \"\$(cat $CG/memory.max)/1073741824\" | bc -l);
       awk '\$1==\"oom_kill\"{printf \"%s\t\", \$2}' $CG/memory.events; echo \"\$a \$b\" | awk '{printf \"%.0f\t%.0f\", (\$3-\$1)/5, (\$4-\$2)*512/5e6}'" > /tmp/q79 2>/dev/null
    ph=$(ps -eo args | grep -q "[r]un_one.py --engine qdrant --dataset deep1b1b --ef 64" && { pgrep -f "[r]un_one.py --engine qdrant" | wc -l | awk '{print ($1>1)?"timed":"warmup"}'; } || echo idle)
    echo -e "$(date -u +%FT%TZ)\t$ph\t$(cat /tmp/q79)" >> $TSV
    sleep 10
  done
}
rm -f $X/logs/79.stop; sampler & SP=$!
sleep 60    # idle baseline
log "79: Qdrant 1B ef64 C=1 re-run with memory sampling -> $TSV"
export QB_TIMEOUT=7200 QB_VEC_ON_DISK=1 QB_HNSW_ON_DISK=1
cd /home/dev_path/corun
out=$(taskset -c 20-23,44-47 .venv/bin/python -u scripts/run_one.py --engine qdrant --dataset deep1b1b \
      --ef 64 --concurrency 1 --repeat r2 --min-seconds 120 --out-dir $OUT --co-run 0 2>&1 | tee -a $LOG | grep -E " qps=" | tail -1)
log "79: ef=64 C=1 r2: ${out:-NO RESULT (see $LOG)}"
sleep 30; touch $X/logs/79.stop; wait $SP
log "=== QDRANT 1B EF64 MEM DONE ==="
