"""Stable Q1 entry points, backed by the shared scheduling coordinator.

The reporting-only single-core denominator is deliberately outside the worker.
"""
from pathlib import Path
from scheduling.api import default_parameters, source_signature
from scheduling.api import solve_case as _solve_case, _run_case
from scheduling.io import read, write, digest, ROOT
from .. import official
from scheduling.configuration import intermediate_artifacts


def solve_case(graph, config, cores, output, *, seconds=None, parameters=None):
    """Preserve the Q1 API and its single shared case budget."""
    return _solve_case(graph, config, cores, output, seconds=seconds,
                       parameters=parameters, problem='q1')


def solve_file(graph,config,cores,output,*,seconds=None,parameters=None,
               local_test_baseline_archive=None,archive_repo_root=None,_verified_test_record=None):
    """One-count compatibility API. Use solve_case for a shared four-count budget.

    Four independent calls start four clocks; never use that loop as a complete
    case-budget benchmark. Pass [2, 3, 4, 5] to solve_case once instead.

    An optional archived baseline is used after solving, for reporting only.
    It never enters the computing worker, candidate decisions or time budget.
    """
    if _verified_test_record is not None and local_test_baseline_archive is None:
        raise ValueError('reporting baseline requires an explicit archive')
    result = _run_case(graph,config,[cores],output,seconds,parameters,single=True)
    row = result['rows'][0]
    row.update(wall_seconds=result['wall_seconds'],accounted_wall_seconds=result['wall_seconds'],
        cpu_seconds=result['cpu_seconds'],shared_preparation_seconds=result['shared_preparation_seconds'])
    if local_test_baseline_archive is not None:
        from common.baseline import load_baseline_catalog
        archive = Path(local_test_baseline_archive).resolve()
        catalog = load_baseline_catalog(archive,[Path(graph)],config,official.CODE,
            repo_root=archive_repo_root or archive.parent.parent)
        entry = catalog[Path(graph).stem]
        row['baseline'] = dict(mode='reporting_only',cycles=entry['makespan_cycles'],reference=entry)
        row['speedup'] = entry['makespan_cycles']/row['makespan'] if row.get('makespan') else None
    if intermediate_artifacts(default_parameters() if parameters is None else parameters):
        write(Path(output)/'summary.json',row)
    return row
