#!/bin/bash
# sample_cg.sh <container> <out.tsv> <stop-file>: every 30 s the container's cgroup memory
# (current/peak/anon/file GB), oom_kill count and cumulative disk read GB. Re-resolves the
# cgroup each tick (containers get recreated). Stops when <stop-file> exists.
CN=$1; O=$2; STOP=$3
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
[ -s $O ] || echo -e "utc\tcur_gb\tpeak_gb\tanon_gb\tfile_gb\toom_kill\tread_gb\tstate" > $O
while [ ! -e $STOP ]; do
  H "id=\$(docker inspect -f '{{.Id}}' $CN 2>/dev/null); st=\$(docker inspect -f '{{.State.Status}}' $CN 2>/dev/null); cg=/sys/fs/cgroup/system.slice/docker-\$id.scope; \
     [ -d \$cg ] && cd \$cg && echo \$(cat memory.current) \$(cat memory.peak) \
       \$(awk '\$1==\"anon\"{a=\$2} \$1==\"file\"{f=\$2} END{print a, f}' memory.stat) \
       \$(awk '\$1==\"oom_kill\"{print \$2}' memory.events) \
       \$(awk '{for(i=2;i<=NF;i++) if(\$i~/^rbytes=/){split(\$i,a,\"=\"); s+=a[2]}} END{print s+0}' io.stat) \$st || echo 0 0 0 0 0 0 \${st:-none}" 2>/dev/null \
  | awk -v t=$(date -u +%FT%TZ) 'NF>=7{g=1073741824; printf "%s\t%.2f\t%.2f\t%.2f\t%.2f\t%s\t%.2f\t%s\n", t,$1/g,$2/g,$3/g,$4/g,$5,$6/g,$7}' >> $O
  sleep 30
done
