"""Random bipartite graph generator from the supplied Q2 model differential tests."""
import random

def make_graph(seed,n=10):
    rng=random.Random(seed);ops=[];ts=[];edges=[];counter=1
    def tensor(s,pos):
        nonlocal counter
        i=counter;counter+=1;ts.append(dict(id=i,size=s,pos=pos));return i
    def op(name,ins,out,c,pipe):
        nonlocal counter
        i=counter;counter+=1;ops.append(dict(id=i,op=name,cycles=c,pipe=pipe));edges.extend(dict(source=t,target=i) for t in ins);edges.append(dict(source=i,target=out));return i
    def inp(size,pos):
        d=tensor(size,'DDR');t=tensor(size,pos);op('COPY_IN',[d],t,1,'PIPE_MTE2');return t
    shared=inp(rng.choice([0,1,17,61,120]),'UB');outs=[];comp=[]
    for j in range(n):
        size=rng.choice([0,1,17,59,60,61,119,121,300]);position=rng.choice(['L1','UB'])
        ins=[shared,inp(size,position)]
        if outs and rng.random()<.8:
            ins+=rng.sample(outs,min(len(outs),rng.choice([1,1,2])))
        t=tensor(size,position);cid=op('MATMUL' if rng.random()<.5 else 'ADD',ins,t,rng.choice([0,1,4,7,13,30,101]),rng.choice(['PIPE_M','PIPE_V']))
        outs.append(t);comp.append(cid)
    consumed={e['source'] for e in edges if e['target'] in comp}
    for t in outs:
        if t not in consumed or rng.random()<.25:
            s=next(x['size'] for x in ts if x['id']==t);dd=tensor(s,'DDR');op('COPY_OUT',[t],dd,1,'PIPE_MTE3')
    return dict(ops=ops,tensors=ts,edges=edges),comp
