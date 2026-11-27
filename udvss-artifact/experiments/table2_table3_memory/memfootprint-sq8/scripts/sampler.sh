#!/usr/bin/env bash
# In-pod memory sampler.  Runs INSIDE the engine container (kubectl exec), so it
# reads the pod's own cgroup v2 files and the pod's own /proc.  Its own cost
# (one bash + awk, a few MB) is inside the measured cgroup; that is noted in the
# results and is negligible against GB-scale numbers.
#
# One line per tick (default 0.5 s), space separated key=value:
#   t_ms        wall clock of the sample (ms since epoch, node clock)
#   cg_cur      memory.current          (bytes; anon + page cache + kernel)
#   cg_peak     memory.peak             (bytes, lifetime of the cgroup; kernel 6.8 cannot reset it)
#   anon file shmem file_mapped kernel   from memory.stat (bytes)
#   pgmajfault workingset_refault_file  from memory.stat (counters; page-cache thrashing)
#   oom oom_kill high max                from memory.events (counters)
#   node_total node_avail                /proc/meminfo of the NODE (not namespaced) in kB
#   procs=name:pid:VmRSS:RssAnon:RssFile:RssShmem|...   (kB, every process in the pod)
INTERVAL=${1:-0.5}
CG=/sys/fs/cgroup
while :; do
  t=$(date +%s%3N)
  cur=$(cat $CG/memory.current)
  peak=$(cat $CG/memory.peak 2>/dev/null || echo -1)
  st=$(awk '$1=="anon"||$1=="file"||$1=="shmem"||$1=="file_mapped"||$1=="kernel"||$1=="pgmajfault"||$1=="workingset_refault_file"{printf "%s=%s ",$1,$2}' $CG/memory.stat)
  ev=$(awk '{printf "%s=%s ",$1,$2}' $CG/memory.events)
  mi=$(awk '$1=="MemTotal:"{t=$2} $1=="MemAvailable:"{a=$2} END{printf "node_total=%s node_avail=%s",t,a}' /proc/meminfo)
  pr=$(for s in /proc/[0-9]*/status; do
         awk -v p="${s//[^0-9]/}" '$1=="Name:"{n=$2} $1=="VmRSS:"{r=$2} $1=="RssAnon:"{a=$2} $1=="RssFile:"{f=$2} $1=="RssShmem:"{h=$2}
              END{ if (r+0>2048) printf "%s:%s:%s:%s:%s:%s|",n,p,r,a,f,h }' "$s" 2>/dev/null
       done)
  echo "t_ms=$t cg_cur=$cur cg_peak=$peak $st$ev$mi procs=$pr"
  sleep "$INTERVAL"
done
