"""Recheck the supplied 059/N5 coarse false-promotions; never used by solving."""
import argparse
import json
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from common.artifacts import write_json, file_hash
from q2.official import Settings, read_json
from q2.graph import Graph
from q2.state import build_state
from q2.optimized.approximation import ModelEvaluator
from q2.pipeline import run_official


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-zip',type=Path,required=True)
    parser.add_argument('--graph',type=Path,default=ROOT/'official/data/case_059.json')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): parser.error('fresh output required')
    settings=Settings.read(); graph=Graph(read_json(args.graph),settings); model=ModelEvaluator(graph)
    rows=[]
    with zipfile.ZipFile(args.reference_zip) as archive:
        for index, expected in [(2,(150736,156177,81920)),(5,(150736,157856,0))]:
            member=f'q2_optimized_design/results/probe1/case_059/plans/candidate_{index:03d}.json'
            plan=json.loads(archive.read(member)); state=build_state(graph,plan,preparation=model.preparation)
            coarse=model.evaluate(state,'coarse'); fine=model.evaluate(state)
            calls=[]; actual=run_official(args.graph.resolve(),plan,settings,args.output/f'candidate_{index}',60,calls,'control')
            if actual is None: raise AssertionError('official replay failed')
            assert (coarse.raw_cycles,fine.raw_cycles,fine.spill_bytes)==expected
            assert fine.raw_cycles==actual['makespan']
            rows.append(dict(index=index,coarse=coarse.raw_cycles,refined=fine.raw_cycles,
                             official=actual['makespan'],spill=fine.spill_bytes,calls=calls))
    write_json(args.output/'summary.json',dict(rows=rows,graph_sha256=file_hash(args.graph),
        reference_zip_sha256=file_hash(args.reference_zip),used_by_solver=False))
    print(json.dumps(rows,indent=2))


if __name__=='__main__': main()
