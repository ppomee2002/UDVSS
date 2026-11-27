# Runs ON 109. Exact L2 top-100 for the first NQ Deep1B public queries against
# the 1M / 10M / 100M base prefixes, one pass over base.100M.fbin.
import numpy as np, time
D=96; NQ=1000; K=100; DIR='/home/ubuntu/deep1b'
base=np.memmap(f'{DIR}/base.100M.fbin',dtype=np.float32,mode='r',offset=8,shape=(100_000_000,D))
q=np.fromfile(f'{DIR}/query.public.10K.fbin',dtype=np.float32,offset=8).reshape(-1,D)[:NQ].astype(np.float32)
qq=(q*q).sum(1,keepdims=True)
bd=np.full((NQ,0),np.inf,np.float32); bi=np.zeros((NQ,0),np.int64)
marks={1_000_000:'1m',10_000_000:'10m',100_000_000:'100m'}
B=1_000_000; t=time.time()
for s in range(0,100_000_000,B):
    b=np.asarray(base[s:s+B])
    d=qq-2*q@b.T+(b*b).sum(1)[None,:]
    p=np.argpartition(d,K-1,axis=1)[:,:K]
    cd=np.concatenate([bd,np.take_along_axis(d,p,1)],1); ci=np.concatenate([bi,p+s],1)
    o=np.argpartition(cd,K-1,axis=1)[:,:K]; bd=np.take_along_axis(cd,o,1); bi=np.take_along_axis(ci,o,1)
    if s+B in marks:
        o=np.argsort(bd,1); gi=np.take_along_axis(bi,o,1).astype(np.int32)
        out=np.empty((NQ,K+1),np.int32); out[:,0]=K; out[:,1:]=gi
        out.tofile(f'{DIR}/deep{marks[s+B]}_gt_q{NQ}.ivecs'); print(marks[s+B],'done',round(time.time()-t),'s',flush=True)
