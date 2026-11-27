#!/bin/bash
# every 10 min during the Milvus 100M run: container memory (docker stats), host
# available memory, minio volume size, host disk free, last progress line.
R=/home/dev_path/experiments/scale-deep; O=$R/engines/logs/milvus_deep1b100m_mem.tsv
H() { ssh -o BatchMode=yes -p 31234 ubuntu@127.0.0.1 "$@"; }
until grep -q "milvus healthy\|FATAL milvus100m" $R/chain.log; do sleep 60; done
[ -s $O ] || echo -e "utc\tmilvus_mem\tminio_mem\thost_avail_gb\tminio_gb\thost_free\tlast_log" > $O
while ! grep -q "=== MILVUS100M DONE\|FATAL milvus100m" $R/chain.log; do
  st=$(H "docker stats --no-stream --format '{{.Name}} {{.MemUsage}}'" 2>/dev/null)
  mv=$(echo "$st" | awk '/milvus-standalone|standalone/{print $2; exit}'); mi=$(echo "$st" | awk '/minio/{print $2; exit}')
  av=$(H "free -g | awk '/Mem:/{print \$7}'"); mg=$(H "du -sh /home/ubuntu/milvus-bench/volumes/minio | cut -f1")
  df=$(H "df -h /home/ubuntu | awk 'NR==2{print \$4}'"); ll=$(tail -1 $R/engines/logs/milvus_deep1b100m.log 2>/dev/null | cut -c1-90)
  echo -e "$(date -u +%FT%TZ)\t$mv\t$mi\t$av\t$mg\t$df\t$ll" >> $O
  sleep 600
done
