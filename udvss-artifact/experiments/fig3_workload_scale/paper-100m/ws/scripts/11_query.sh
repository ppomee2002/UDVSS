#!/bin/bash
# Usage: 11_query.sh 1m|10m|100m full|tree M REP -- one measured run (full PG scan or unlimited tree), NQ=1, C=1,
# warmup 50 + 500 measured (as workload-scale-sq8 11_query.sh). Appends to runs.csv.
source /home/dev_path/experiments/paper-100m/ws/scripts/env.sh
set -e
S=$1 MODE=$2 M=$3 REP=$4; POOL=$(ws_pool $S)
C=${5:-1}   # 1 for latency rows
LABEL=${S}_${MODE}_M${M}_r${REP}; OUT=$RES/runs/$LABEL-$(date -u +%Y%m%d-%H%M%S); mkdir -p $RES/runs
ARGS="--operation fanout-query --pool $POOL --index ${POOL}_index $(ws_idxargs $WS_PG) \
$QUANT $(ws_qargs_$MODE $S | tr '\n' ' ') $(ws_dset $S) --m $M --query-limit $WS_NQ --query-concurrency $C \
--warmup-rounds 0 --warmup-queries $WS_WARM --rounds 1 --fanout-mode parallel --reply-omit-tree-stats"
echo "[$(date -u +%FT%TZ)] $LABEL: $ARGS" >> $RES/logs/commands.log
taskset -c $CLIENT_CPUS $BB/ceph-vector-bench-native --conf $CEPH_CONF $ARGS > $OUT.log 2>&1 \
  || { log "FATAL: bench failed, see $OUT.log"; tail -5 $OUT.log; exit 1; }
python3 - $OUT $S $MODE $M $REP $(ws_n $S) <<'PY' | tee -a $RES/runs.csv
import sys,re,os
out,s,mode,m,rep,n=sys.argv[1:]
kv=dict(re.findall(r"(\w+)=(\S+)",[l for l in open(out+".log") if l.startswith("FANOUT_QUERY")][0]))
for k in ("failures","missing_objects"):
    if kv.get(k,"0")!="0": sys.exit(f"FATAL {k}={kv[k]}")
row=dict(scale=s,n=n,mode=mode,pg=1024,m=m,rep=rep,queries=kv["completed"],failures=kv["failures"],
  missing=kv.get("missing_objects"),dups=kv.get("duplicate_entries"),
  recall=kv.get("recall_at_top_k"),candidates=kv["avg_total_matching_records_per_query"],
  mean_us=kv["logical_latency_us_avg"],p50_us=kv["logical_latency_us_p50"],
  p95_us=kv.get("logical_latency_us_p95"),p99_us=kv["logical_latency_us_p99"],
  osd_req=kv.get("osd_requests_per_query"),
  log=os.path.basename(out)+".log")
csv=os.path.join(os.path.dirname(os.path.dirname(out)),"runs.csv")
if not os.path.exists(csv) or os.path.getsize(csv)==0: print(",".join(row))
print(",".join(str(v) for v in row.values()))
PY
