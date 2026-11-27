#!/bin/bash
# Runs ON 109. Deep1B first 100M base rows (fbin range) + 10K public queries,
# 16 parallel ranges, then fbin -> fvecs (100M prefix serves 1M/10M/100M via --base-limit).
set -eu
D=/home/ubuntu/deep1b; cd $D
U=https://storage.yandexcloud.net/yandex-research/ann-datasets/DEEP
N=100000000; DIM=96
TOTAL=$((8 + N*DIM*4)); P=16; CH=$(( (TOTAL + P - 1) / P ))
curl -sS --retry 10 -o query.public.10K.fbin $U/query.public.10K.fbin
for i in $(seq 0 $((P-1))); do
  s=$((i*CH)); e=$(( (i+1)*CH - 1 )); [ $e -ge $TOTAL ] && e=$((TOTAL-1))
  ( want=$((e-s+1))
    until [ -f part.$i ] && [ $(stat -c %s part.$i) -eq $want ]; do
      have=0; [ -f part.$i ] && have=$(stat -c %s part.$i)
      curl -sS --retry 10 -r $((s+have))-$e $U/base.1B.fbin >> part.$i || sleep 5
    done ) &
done
wait
cat $(for i in $(seq 0 $((P-1))); do echo part.$i; done) > base.100M.fbin
[ $(stat -c %s base.100M.fbin) -eq $TOTAL ] && rm -f part.*
python3 - <<PY
import numpy as np
N=$N; D=$DIM
def fb2fv(src, dst, n):
    m=np.memmap(src,dtype=np.float32,mode='r',offset=8,shape=(n,D))
    with open(dst,'wb') as f:
        for s in range(0,n,1_000_000):
            blk=np.asarray(m[s:s+1_000_000]); out=np.empty((len(blk),D+1),dtype=np.float32)
            out[:,0]=np.frombuffer(np.int32(D).tobytes(),dtype=np.float32)[0]; out[:,1:]=blk
            out.tofile(f)
fb2fv('base.100M.fbin','deep100m_base.fvecs',N)
fb2fv('query.public.10K.fbin','deep1b_query.fvecs',10000)
PY
echo DONE > download.done
