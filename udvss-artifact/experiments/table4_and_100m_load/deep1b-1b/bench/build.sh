#!/bin/bash
# build.sh -- crush-exp2 source + patch_1b.py, compiled with the seastore-batch
# build's own flags but -O3 -march=native -ffp-contract=off (no FMA: the
# distance arithmetic stays bit-identical to the -O2 generic build), linked
# against that build's libraries. Never touches the seastore-batch tree.
set -e
W=/home/dev_path/experiments/deep1b-1b/bench
B=/home/dev_path/ceph-vectornode-seastore-batch/build
D=$B/src/tools/CMakeFiles/ceph-vector-bench-native.dir; F=$D/flags.make
cp /home/dev_path/experiments/crush-exp2/bench/src/tools/ceph_vector_bench_native.cc $W/src/tools/
python3 $W/patch_1b.py $W/src/tools/ceph_vector_bench_native.cc
DEF=$(grep '^CXX_DEFINES' $F | cut -d= -f2-); INC=$(grep '^CXX_INCLUDES' $F | cut -d= -f2-)
FL=$(grep '^CXX_FLAGS' $F | cut -d= -f2- | sed 's/-O2/-O3 -march=native -ffp-contract=off/')
echo "$FL" > $W/obj/BUILD_FLAGS
cd $W/obj
/usr/bin/g++-13 $DEF $INC $FL -c $W/src/tools/ceph_vector_bench_native.cc -o b1b.o
LK=$(sed -e "s#CMakeFiles/ceph-vector-bench-native.dir/ceph_vector_bench_native.cc.o#$W/obj/b1b.o#" \
         -e "s#-o ../../bin/ceph-vector-bench-native#-o $W/obj/ceph-vector-bench-native.new#" \
         -e "s#\.\./\.\./#$B/#g" $D/link.txt)
(cd $D/../.. && eval "$LK")
md5sum $W/obj/ceph-vector-bench-native.new
