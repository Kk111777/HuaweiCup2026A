"""Recompute the serial base API and compare it with the completed base archive."""
import argparse
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from q1 import initialize,official
from q1.plan import normalized
from q1.unified.io import read,write


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--cases',type=int,nargs='+',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();rows=[]
    settings=official.Settings.read(a.data/'config.txt')
    for case in a.cases:
        if case==76:raise ValueError('case76 excluded from staged verification')
        name=f'case_{case:03d}';raw=read(a.data/f'{name}.json')
        for n in (2,5):
            t=time.perf_counter()
            result=initialize(raw,n,settings,candidate_jobs=1)
            folder=a.archive/name/f'N{n}'
            assert result.plan==read(folder/'plan.json'),(name,n,'base plan')
            assert normalized(result.evaluation)==normalized(read(folder/'result.json')),(name,n,'base result')
            rows.append(dict(case=name,cores=n,exact=True,cycles=result.evaluation['makespan'],seconds=time.perf_counter()-t))
            write(a.output,dict(status='running',rows=rows));print(rows[-1],flush=True)
    write(a.output,dict(status='passed',rows=rows))


if __name__=='__main__':main()
