# Ceph arm of the FP32 vs SQ8 memory experiment.  ARM=fp32|int8 must be set.
set -u
: "${ARM:?ARM=fp32|int8}"
export EXP=/home/dev_path/experiments/memfootprint-sq8/ceph
export SRC=/home/dev_path/ceph-vectornode-seastore-batch   # workload-bench build: --quantize + --pivot-refine-iters
export BUILD=$SRC/build BIN=$SRC/build/bin
export LD_LIBRARY_PATH=$BUILD/lib
export VDEST=/home/dev_path/vstart/memfp-$ARM          # created (and later removed) by this experiment only
export CEPH_CONF=$VDEST/ceph.conf CEPH_KEYRING=$VDEST/keyring
export DATA=/home/dev_path/ceph/cohere1m
export LIST_BLOCK_BYTES=16384                           # as ffpivot-smp8-1m / dual-repr
export DEVICE_SIZE=28G                                  # non-sparse; 16M segments -> ~220 per reactor shard
if [ "$ARM" = fp32 ]; then
  export CEPH_PORT_BASE=8401 OSD_CPUSET=0-7  QUANT=float32
else
  export CEPH_PORT_BASE=8501 OSD_CPUSET=8-15 QUANT=int8
fi
export CLIENT_CPUS=16-23,40-47                          # no SMT sibling of either OSD (0-15 <-> 24-39)
export PGNUM=1024
# published pivot geometry (dual-repr env.sh / ffpivot-smp8-1m)
export IDXARGS="--routing-mode pivot --pg-num $PGNUM --k 10 --l 4 --seed 1315423911 --d 1 --pivot-build-sample 16384 --pivot-refine-iters 3 --pivot-probe-budget 96"
log() { echo "[$(date -u +%H:%M:%S)] [$ARM] $*"; }
