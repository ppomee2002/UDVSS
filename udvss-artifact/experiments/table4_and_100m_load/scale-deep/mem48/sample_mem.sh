#!/bin/bash
# every 30 s: Vespa container cgroup memory (current/peak, anon/file), oom_kill count, disk read bytes.
D=/home/dev_path/experiments/scale-deep/mem48; O=$D/vespa_mem48_cgroup.tsv
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
[ -s $O ] || echo -e "utc\tcur_gb\tpeak_gb\tanon_gb\tfile_gb\toom_kill\tread_gb\tphase" > $O
while ! grep -q "=== VESPA MEM48 DONE" $D/driver.log 2>/dev/null; do
  cg=$(tail -1 $D/state.cg 2>/dev/null)
  if [ -n "$cg" ]; then
    H "cd $cg 2>/dev/null && echo \$(cat memory.current) \$(cat memory.peak 2>/dev/null || echo 0) \
       \$(awk '\$1==\"anon\"{a=\$2} \$1==\"file\"{f=\$2} END{print a, f}' memory.stat) \
       \$(awk '\$1==\"oom_kill\"{print \$2}' memory.events) \
       \$(awk '{for(i=2;i<=NF;i++) if(\$i~/^rbytes=/){split(\$i,a,\"=\"); s+=a[2]}} END{print s+0}' io.stat)" \
    | awk -v t=$(date -u +%FT%TZ) -v ph="$(grep -oE '\((a|b|prep)\)' $D/driver.log | tail -1)" \
        'NF>=6{g=1073741824; printf "%s\t%.2f\t%.2f\t%.2f\t%.2f\t%s\t%.2f\t%s\n", t,$1/g,$2/g,$3/g,$4/g,$5,$6/g,ph}' >> $O
  fi
  sleep 30
done
