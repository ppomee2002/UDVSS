# osd-scale-deep: exp 1 (2026-10-19) -- QPS vs OSD count 1/4/8/12,
# Deep1B prefix 1M -> 10M -> 100M. Runs ON 109 (everything local: the 94<->109
# link is 1 GbE and would cap QPS before 12 OSDs do).
#   OSD/bench : bundle of node94's seastore-batch build (same binaries as
#               scale-deep d100m / mem48), see bundle/SOURCE
#   mon/mgr/CLI: 109's native seastore-batch build (vstart), as hier-lsh-1m
# Per-OSD resources are FIXED (6 reactors = 3 physical cores + HT siblings on
# one NUMA node), so total CPU grows with the OSD count (scale-out).
set -u
export X=$HOME/osdscale
export SRC=/home/dev_path/ceph-vectornode-seastore-batch
export OB=$SRC/build/bin
export BUNDLE=$X/bundle
export VDEST=$X/cluster
export CONF=$VDEST/ceph.conf
export ODIR=$X/osd
export PORT=7640
export IP=${NODE109_IP:?set NODE109_IP}
export OSD_PORT_MIN=7400 OSD_PORT_MAX=7599
export RPO=6                       # reactors per OSD
export LOAD_CONC=${LOAD_CONC:-256}
export MIN_SECONDS=${MIN_SECONDS:-120}
export BATCH=1024
export PROBE_BUDGET=96
export CMDLOG=$X/logs/commands.log

# Client: physical cores 38..63 (both NUMA nodes) + siblings = 52 threads.
# CPUs 0/1/64/65 are left to the system and mon/mgr.
export CLIENT_CPUS=$(echo $(seq 38 63) $(seq 102 127) | tr ' ' ,)
export MON_CPUS=0,1,64,65

# OSD i -> NUMA node i%2, slot i/2 (0..5): physical cores node+2*(1+3*slot+j),
# j=0..2, plus HT siblings (+64). 12 OSDs use physical cores 2..37.
osd_cpus() {
  local i=$1 node=$(( $1 % 2 )) slot=$(( $1 / 2 )) j c out=""
  for j in 0 1 2; do c=$(( node + 2 * (1 + 3 * slot + j) )); out="$out,$c,$(( c + 64 ))"; done
  echo "${out#,}"
}

# Scale -> dataset / PG / pivot routing (= scale-deep arms d1m, d10m_pg4096, d100m_pg16384).
scale_vars() {
  local s=$1 D=$X/data/deep1b$1
  SCALE=$s; BENCH_BIN=
  DSET="--base $D/deep1b${s}_base.fvecs --query $D/deep1b${s}_query.fvecs --groundtruth $D/deep1b${s}_groundtruth.ivecs"
  case $s in
    1m)   BASE_COUNT=1000000;   PG=1024;  TOT_MEM=6;  DEV_TOT=48
          ROUTE="--pivot-build-sample 16384 --pivot-refine-iters 3" ;;
    10m)  BASE_COUNT=10000000;  PG=4096;  TOT_MEM=16; DEV_TOT=96
          ROUTE="--pivot-build-sample 262144 --pivot-refine-iters 25 --pivot-balance-eps 0.1 --pivot-place-cap-eps 0.1 --pivot-model-file $D/pivot_model_pg4096.bin" ;;
    100m) BASE_COUNT=100000000; PG=16384; TOT_MEM=56; DEV_TOT=256
          ROUTE="--pivot-build-sample 1048576 --pivot-refine-iters 25 --pivot-balance-eps 0.1 --pivot-place-cap-eps 0.1 --pivot-model-file $D/pivot_model_pg16384.bin" ;;
    # experiment 2 CRUSH-style baseline: the 100m data, PG = rjenkins("vec_<row>"),
    # random probe order; hash-placement bench (experiments/crush-exp2/bench)
    100mh) D=$X/data/deep1b100m
          DSET="--base $D/deep1b100m_base.fvecs --query $D/deep1b100m_query.fvecs --groundtruth $D/deep1b100m_groundtruth.ivecs"
          BASE_COUNT=100000000; PG=16384; TOT_MEM=56; DEV_TOT=256
          ROUTE="--hash-placement"; BENCH_BIN=ceph-vector-bench-native-hash ;;
    *) echo "unknown scale $s" >&2; return 1 ;;
  esac
  POOL=os_d$s; INDEX=${POOL}_index
  IDXARGS="--routing-mode pivot --k 10 --l 4 --seed 1315423911 --d 1 --top-k 10 $ROUTE --pg-num $PG --pivot-probe-budget $PROBE_BUDGET"
}
# Per-OSD seastar memory and device: total/N + headroom, with floors
# (>= 8G device per reactor: smaller devices ran out of journal segments).
osd_mem() { local n=$1 m; m=$(( (TOT_MEM + n - 1) / n + 4 )); [ $m -lt 10 ] && m=10; echo ${m}G; }
osd_dev() { local n=$1 d; d=$(( (DEV_TOT + n - 1) / n )); [ $d -lt 48 ] && d=48; echo ${d}G; }

log() { echo "[$(date -u +%FT%TZ)] $*"; }
die() { log "FATAL: $*"; exit 1; }
ceph_() { $OB/ceph -c $CONF "$@" 2>/dev/null; }
bench() {
  echo "[$(date -u +%FT%TZ)] bench $*" >> $CMDLOG
  taskset -c $CLIENT_CPUS $BUNDLE/run.sh ${BENCH_BIN:-ceph-vector-bench-native} --conf $CONF "$@"
}
num_up() { ceph_ osd stat -f json | python3 -c 'import json,sys;print(json.load(sys.stdin)["num_up_osds"])' 2>/dev/null || echo 0; }
# PIDs of THIS experiment's daemons (cmdline names $X/); never pgrep -f over ssh.
osd_pids() { ps -eo pid,args | grep "[o]sdscale/bundle/bin/crimson-osd" | awk '{print $1}'; }
mon_pids() { ps -eo pid,args | grep "[o]sdscale/cluster/ceph.conf" | grep -E "ceph-mon|ceph-mgr" | awk '{print $1}'; }
