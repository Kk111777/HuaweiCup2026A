"""Differential validation against the preserved implementation, not score oracles."""
import argparse
import importlib.util
import importlib
import json
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'tests'))
from test_q1_initialization import graph_of, SETTINGS
from q1 import initialize, official
from q1.model import Graph
from q1.graph import GraphIndex
from q1.improvement import operators, schedule
from q1.unified.api import default_parameters
from q1.unified.service import SearchServices
from q1.unified.io import read, write
from q1.plan import normalized


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--data',type=Path)
    p.add_argument('--cases',nargs='*',type=int,default=[])
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    spec=importlib.util.spec_from_file_location('reference_q1',a.reference/'src/q1/__init__.py',
        submodule_search_locations=[str(a.reference/'src/q1')])
    old=importlib.util.module_from_spec(spec);sys.modules['reference_q1']=old;spec.loader.exec_module(old)
    oldgraph=importlib.import_module('reference_q1.refinement.graph')
    oldops=importlib.import_module('reference_q1.refinement.operators')
    oldschedule=importlib.import_module('reference_q1.refinement.schedule')
    rng=random.Random(20260925)
    rows=[]
    for case in range(48):
        n=rng.randrange(2,23)
        links=[(i,j) for i in range(n) for j in range(i+1,n) if rng.random()<.12]
        raw=graph_of(n,links,cycles=[rng.randrange(0,150) for _ in range(n)])
        cores=2+case%4
        start=time.perf_counter()
        g=Graph(raw); index=GraphIndex(g,SETTINGS)
        assert index.preds is g.pred and index.succs is g.succ and index.inputs is g.inputs
        current=initialize(raw,cores,SETTINGS,candidate_jobs=1)
        reference=old.initialize(raw,cores,SETTINGS,candidate_jobs=1)
        assert current.plan==reference.plan,('base plan changed',case)
        assert normalized(current.evaluation)==normalized(reference.evaluation),case
        og=oldgraph.Graph(raw);cfg=SearchServices(g,SETTINGS).settings_dict
        assert operators.initial_candidates(g,cores,cfg,default_parameters())==oldops.initial_candidates(og,cores,cfg,default_parameters()),('random initial operators',case)
        blocks=[frozenset([v]) for v in g.order]
        for priority in ('critical','fifo','largest'):
            assert schedule.schedule(g,blocks,cores,cfg,priority=priority)==oldschedule.schedule(og,blocks,cores,cfg,priority=priority)
        rows.append(dict(kind='random',case=case,cores=cores,exact=True,seconds=time.perf_counter()-start))
    try:
        initialize(raw,2,SETTINGS,candidate_jobs=2)
        raise AssertionError('within-case parallelism allowed')
    except ValueError as e:
        assert 'parallelism' in str(e)
    for case in a.cases:
        if case==76:raise ValueError('exclude case76 from staged validation')
        raw=read(a.data/f'case_{case:03d}.json');g=Graph(raw);og=oldgraph.Graph(raw)
        cfg=SearchServices(g,SETTINGS).settings_dict
        for cores in (2,5):
            start=time.perf_counter()
            initial=operators.initial_candidates(g,cores,cfg,default_parameters())
            assert initial==oldops.initial_candidates(og,cores,cfg,default_parameters()),('initial',case,cores)
            plan=initial[0][1]
            result=official.evaluate(raw,plan,SETTINGS)
            assert operators.local_candidates(g,plan,result,cfg,default_parameters())==oldops.local_candidates(og,plan,result,cfg,default_parameters()),('local',case,cores)
            rows.append(dict(kind='real_kernel',case=case,cores=cores,exact=True,seconds=time.perf_counter()-start))
            write(a.output,dict(status='running',rows=rows))
            print(json.dumps(rows[-1]),flush=True)
    write(a.output,dict(status='passed',random_seed=20260925,rows=rows))
    print(f'Passed {len(rows)} differential checks')


if __name__=='__main__':main()
