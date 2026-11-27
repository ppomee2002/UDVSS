#!/bin/bash
# Usage: ROUTE=lsh|pivot 13_run_sq8.sh FAMILY PG M MODE REP [NQUERY [WARMUP]]
# SQ8 variant of 03_run.sh; M is explicit (not tied to PG). Rows go to
# results_sq8/runs.csv with route/pg/m/osd columns.
#   FAMILY  CS (PG sweep, (a)(b)) or CQ (OSD sweep, (c)(d))
#   MODE    ID (ids+distances) or VEC (also raw vectors)
# One measured run: NQUERY queries (default 3000) after WARMUP queries
# (default 500, as Milvus), C=1, NQ=1, M=PG (every PG probed). Traffic = OSD-port
# rx+tx on 109 between the measure hook's begin/end, minus the idle rate.
source /home/dev_path/experiments/ceph-s-qn-scaling/scripts/env_sq8.sh
set -e
FAM=$1 PG=$2 M=$3 MODE=$4 REP=$5 NQ=${6:-$NQUERY} WARM=${7:-500}
POOL=$(sq_pool $PG)
N=$(ceph_ osd stat -f json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["num_up_osds"])')
LABEL=${FAM}_${ROUTE}_${MODE}_PG${PG}_M${M}_OSD${N}_r${REP}
TS=$(date -u +%Y%m%d-%H%M%S)
OUT=$RES/runs/$LABEL-$TS
mkdir -p $RES/runs
VEC=""; [ "$MODE" = VEC ] && VEC="--reply-include-vector"

# idle rate over 10 s right before the run
$EXP/scripts/netctr.sh snap $OUT.idle0.json; sleep 10; $EXP/scripts/netctr.sh snap $OUT.idle1.json

cd $SQ_SRC/build
taskset -c $CLIENT_CPUS $SQ_BIN/ceph-vector-bench-native --conf $CEPH_CONF \
  --operation fanout-query --pool $POOL --index ${POOL}_index $(sq_idxargs $PG) \
  $QUANT $(sq_qargs) $DSET --m $M --query-limit $NQ --query-concurrency 1 \
  --warmup-rounds 0 --warmup-queries $WARM --rounds 1 --fanout-mode parallel \
  --reply-omit-tree-stats $VEC --measure-hook "$EXP/scripts/netctr.sh $OUT" \
  > $OUT.log 2>&1 || { log "FATAL: bench failed, see $OUT.log"; tail -5 $OUT.log; exit 1; }

python3 - $OUT $LABEL $FAM $MODE $PG $N $REP $ROUTE $M <<'PY' | tee -a $RES/runs.csv
import json,sys,re,os
out,label,fam,mode,pg,n,rep,route,m=sys.argv[1:]
ld=lambda s: json.load(open(f"{out}.{s}.json"))
i0,i1,b,e=ld("idle0"),ld("idle1"),ld("begin"),ld("end")
line=[l for l in open(out+".log") if l.startswith("FANOUT_QUERY")][0]
kv=dict(re.findall(r"(\w+)=(\S+)",line))
nq=int(kv["completed"]); win=e["t"]-b["t"]; idle_win=i1["t"]-i0["t"]
row={"label":label,"family":fam,"route":route,"mode":mode,"pg":int(pg),"m":int(m),"osd":int(n),"rep":int(rep),
     "queries":nq,"window_s":round(win,3)}
for d in ("rx","tx"):
    k="osd_"+d
    by=e[k]["bytes"]-b[k]["bytes"]; pk=e[k]["packets"]-b[k]["packets"]
    idle_by=(i1[k]["bytes"]-i0[k]["bytes"])/idle_win*win
    idle_pk=(i1[k]["packets"]-i0[k]["packets"])/idle_win*win
    # +14 B/packet: Ethernet header, which the Milvus pod-eth0 counters include
    row[f"{d}_kb_per_query"]=round((by-idle_by+14*(pk-idle_pk))/nq/1024,4)
    row[f"{d}_idle_kb_per_query"]=round(idle_by/nq/1024,4)
row["traffic_kb_per_query"]=round(row["rx_kb_per_query"]+row["tx_kb_per_query"],4)
row["mean_us"]=float(kv["logical_latency_us_avg"])
row["p50_us"]=float(kv["logical_latency_us_p50"]); row["p99_us"]=float(kv["logical_latency_us_p99"])
row["recall"]=float(kv.get("recall_at_top_k","nan"))
row["reply_bytes_per_query"]=float(kv["reply_bytes_per_query"])
row["reply_vector_bytes_per_query"]=float(kv["reply_vector_bytes_per_query"])
row["candidates"]=float(kv.get("avg_total_matching_records_per_query","nan"))
row["failures"]=int(kv["failures"]); row["log"]=os.path.basename(out)+".log"
json.dump(row,open(out+".row.json","w"),indent=1)
cols=list(row)
hdr=os.path.join(os.path.dirname(os.path.dirname(out)),"runs.csv")
if not os.path.exists(hdr) or os.path.getsize(hdr)==0: print(",".join(cols))
print(",".join(str(row[c]) for c in cols))
PY
