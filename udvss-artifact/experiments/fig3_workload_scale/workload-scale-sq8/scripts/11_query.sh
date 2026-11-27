#!/bin/bash
# Usage: 11_query.sh 1m|10m|100m tree|full M -- one measured run, NQ=1, C=1.
source /home/dev_path/experiments/workload-scale-sq8/scripts/env_ws.sh
set -e
S=$1 MODE=$2 M=$3; POOL=$(ws_pool $S)
# 100m full M=96/384 (about 1 h each) are skipped only if 100m full == tree
# (same candidates) at M=8 and M=32, i.e. the tree budget never cut a PG short.
# At 100m a PG averages ~59 onodes against budget 64, so this is checked, not assumed.
if [ "$S" = 100m ] && [ "$MODE" = full ] && [ "$M" -gt 32 ]; then
  same=$(python3 - $RES/runs.csv <<'PY'
import csv,sys
r=[x for x in csv.DictReader(open(sys.argv[1])) if x["scale"]=="100m"]
t={x["m"]:x["candidates"] for x in r if x["mode"]=="tree"}; f={x["m"]:x["candidates"] for x in r if x["mode"]=="full"}
print(int(all(m in f and f[m]==t.get(m) for m in ("8","32"))))
PY
)
  [ "$same" = 1 ] && { log "SKIP 100m full M$M: at 100m full == tree for M=8 and M=32"; exit 0; }
fi
LABEL=${S}_${MODE}_M${M}; OUT=$RES/runs/$LABEL-$(date -u +%Y%m%d-%H%M%S); mkdir -p $RES/runs
cd $SQ_SRC/build
taskset -c $CLIENT_CPUS $SQ_BIN/ceph-vector-bench-native --conf $CEPH_CONF \
  --operation fanout-query --pool $POOL --index ${POOL}_index $(sq_idxargs $WS_PG) \
  $QUANT $(ws_qargs $MODE) $(ws_dset $S) --m $M --query-limit $WS_NQ --query-concurrency 1 \
  --warmup-rounds 0 --warmup-queries $WS_WARM --rounds 1 --fanout-mode parallel \
  --reply-omit-tree-stats > $OUT.log 2>&1 || { log "FATAL: bench failed, see $OUT.log"; tail -5 $OUT.log; exit 1; }
python3 - $OUT $S $MODE $M $(ws_n $S) <<'PY' | tee -a $RES/runs.csv
import sys,re,os
out,s,mode,m,n=sys.argv[1:]
kv=dict(re.findall(r"(\w+)=(\S+)",[l for l in open(out+".log") if l.startswith("FANOUT_QUERY")][0]))
row=dict(scale=s,n=n,mode=mode,pg=1024,m=m,queries=kv["completed"],failures=kv["failures"],
  recall=kv.get("recall_at_top_k"),candidates=kv["avg_total_matching_records_per_query"],
  mean_us=kv["logical_latency_us_avg"],p50_us=kv["logical_latency_us_p50"],p99_us=kv["logical_latency_us_p99"],
  log=os.path.basename(out)+".log")
hdr=os.path.join(os.path.dirname(os.path.dirname(out)),"runs.csv")
if not os.path.exists(hdr) or os.path.getsize(hdr)==0: print(",".join(row))
print(",".join(str(v) for v in row.values()))
PY
