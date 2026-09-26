"""Import final Q2 answers, or automatically run the unchanged Q2 new baseline."""
import csv
import hashlib
import json
from pathlib import Path
import sys
import time
from zipfile import ZipFile

from .artifacts import file_hash, write_json
from q2.official import ROOT, read_json
from .runtime import run_command

DEFAULT_SOURCE = ROOT / 'results/q2_repair_full_20260926'
SINGLECORE_SOURCE = ROOT / 'results/problem_1_singlecore'


def check_manifest(directory, required):
    path = directory / 'manifest.sha256'
    if not path.is_file():
        return False
    hashes = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        sha, name = line.split('  ', 1)
        if name in hashes:
            raise ValueError('duplicate Q2 manifest member')
        hashes[name] = sha
    for name in required:
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or name not in hashes or not path.is_file() or file_hash(path) != hashes[name]:
            raise ValueError(f'Q2 source manifest mismatch: {name}')
    return True


def fixed_singlecore(graph, settings):
    archive = SINGLECORE_SOURCE
    info_path = archive / 'run_info.json'
    if not info_path.is_file():
        return None
    info = read_json(info_path)
    expected = {Path(k).name: v for k, v in info.get('input_sha256', {}).items()}
    if expected.get(graph.name) != file_hash(graph):
        return None
    member = f'{graph.stem}/{graph.stem}_singlecore_res.json'
    required = ['run_info.json', 'summary.csv', member,
                f'{graph.stem}/{graph.stem}_singlecore_trace.json', f'{graph.stem}/{graph.stem}_singlecore_log.txt']
    if not check_manifest(archive, required):
        return None
    official = {k.removeprefix('official/'): v for k, v in info['official_code_sha256'].items()}
    expected_official = {k: v for k, v in settings.scene.hashes.items() if k.startswith('code/')}
    if official != expected_official or info['config']['sha256'] != file_hash(settings.scene.config) or info.get('status') != 'complete':
        raise ValueError('fixed single-core source environment/status mismatch')
    result = read_json(archive / member)
    if result.get('execution_mode') != 'singlecore' or result['num_cores'] != 1:
        raise ValueError('fixed single-core source is not the official fixed baseline')
    return dict(kind='fixed_singlecore', source_algorithm='official_fixed_singlecore',
                source=str(archive / member), source_result_sha256=file_hash(archive / member),
                generated_seconds=0.0, frozen_objective=True,
                expected_objective=[result['makespan'], result['data_movement_bytes']['added_copy_bytes']])


def _archived(graph_path, cores, source, settings, explicit):
    directory = source.parent if source.is_file() else source
    archive = source if source.is_file() else source / 'plans.zip'
    if not archive.is_file():
        return None
    expected = None
    provenance = None
    trusted = check_manifest(directory, [archive.name, 'details.csv', 'provenance.json'])
    provenance_path = directory / 'provenance.json'
    details_path = directory / 'details.csv'
    if provenance_path.is_file():
        provenance = read_json(provenance_path)
        expected_hash = provenance.get('input_sha256', {}).get(graph_path.name)
        if expected_hash is None or expected_hash != file_hash(graph_path):
            if explicit:
                raise ValueError('Q2 archive input hash does not match the requested graph')
            return None
        if provenance.get('official_sha256') != settings.scene.hashes:
            raise ValueError('Q2 archive official code/config mismatch')
    if details_path.is_file():
        with details_path.open(encoding='utf-8-sig', newline='') as stream:
            matches = [r for r in csv.DictReader(stream)
                       if r['case'] == graph_path.stem and int(r['num_cores']) == cores]
        if len(matches) > 1:
            raise ValueError('duplicate case/core in Q2 archive')
        if matches:
            expected = matches[0]
            if expected['status'] != 'SUCCESS_VERIFIED':
                raise ValueError('Q2 archive result is not verified')
    name = expected['plan_zip_member'] if expected else f'N{cores}/{graph_path.stem}_plan.json'
    with ZipFile(archive) as reader:
        if name not in reader.namelist():
            return None
        content = reader.read(name)
    sha = hashlib.sha256(content).hexdigest()
    if expected and sha != expected['plan_sha256']:
        raise ValueError('Q2 archived plan was modified')
    return json.loads(content), dict(kind='published_q2', source=str(archive), member=name,
        source_plan_sha256=sha, expected_objective=[int(expected['makespan_cycles']), int(expected['added_copy_bytes'])] if expected else None,
        frozen_objective=bool(trusted and expected and provenance),
        source_result_sha256=expected.get('result_sha256') if expected else None,
        source_algorithm=provenance.get('algorithm', {}).get('algorithm_version', 'supplied') if provenance else 'supplied', generated_seconds=0.0)


def find_input(graph_path, cores, source, settings):
    explicit = source is not None
    source = Path(source) if explicit else DEFAULT_SOURCE
    if not source.exists():
        if explicit:
            raise FileNotFoundError(source)
        return None
    if source.suffix.lower() == '.zip' or source.is_dir() and (source / 'plans.zip').is_file():
        return _archived(graph_path, cores, source, settings, explicit)
    paths = [source] if source.is_file() else [source / graph_path.stem / f'N{cores}' / 'plan.json',
             source / f'N{cores}' / f'{graph_path.stem}_plan.json', source / f'N{cores}' / 'plan.json',
             source / 'plan.json']
    for path in paths:
        if not path.is_file():
            continue
        plan = read_json(path)
        if len(plan.get('core_schedules', [])) != cores:
            raise ValueError(f'Q2 input core count mismatch: {path}')
        record = dict(kind='supplied_q2_plan', source=str(path.resolve()),
                      source_plan_sha256=file_hash(path), expected_objective=None,
                      source_algorithm='supplied', generated_seconds=0.0)
        if (path.parent / 'complete.json').is_file():
            from q2.pipeline import reusable
            marker = read_json(path.parent / 'complete.json')
            if not reusable(path.parent, marker.get('fingerprint')):
                raise ValueError('supplied Q2 completion record failed verification')
            identity = read_json(path.parent / 'provenance.json')['identity']
            if identity['graph_sha256'] != file_hash(graph_path) or identity['official_sha256'] != settings.scene.hashes:
                raise ValueError('supplied Q2 input/environment mismatch')
            result = read_json(path.parent / 'result.json')
            record.update(frozen_objective=True, source_result_sha256=file_hash(path.parent / 'result.json'),
                          expected_objective=[result['makespan'], result['data_movement_bytes']['added_copy_bytes']])
        return plan, record
    return None


def resolve(graph_path, cores, source, settings, options, directory):
    graph_path, directory = Path(graph_path), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if cores == 1:
        from singlecore_evaluate import build_singlecore_plan
        plan = build_singlecore_plan(read_json(graph_path))
        record = fixed_singlecore(graph_path, settings) or dict(kind='fixed_singlecore', source_algorithm='official_fixed_singlecore',
                      generated_seconds=0.0, expected_objective=None)
    else:
        found = find_input(graph_path, cores, source, settings)
        if found is not None:
            plan, record = found
        else:
            # The absence of a previous result is a normal path, never a request
            # for the user to manually run Problem 2.
            started = time.monotonic()
            bootstrap = directory / 'q2_bootstrap'
            command = [sys.executable, str(ROOT / 'src/solve_problem2.py'), 'solve', str(graph_path.resolve()),
                       '--cores', str(cores), '--without-baseline', '--output-dir', str(bootstrap),
                       '--reference-root', str(settings.scene.root), '--config', str(settings.scene.config),
                       '--wall-seconds', str(options['q2_seconds']), '--verification-reserve',
                       str(min(60, options['q2_seconds'] * .2))]
            run = run_command(command, timeout=options['q2_seconds'] + 15, text=True, encoding='utf-8', errors='replace')
            (directory / 'q2_stdout.txt').write_text(run.stdout, encoding='utf-8')
            (directory / 'q2_stderr.txt').write_text(run.stderr, encoding='utf-8')
            if run.returncode:
                raise RuntimeError(f'Q2 prerequisite failed, Q3 not started: {run.stderr[-1800:]}')
            final = bootstrap / graph_path.stem / f'N{cores}'
            if not (final / 'complete.json').is_file():
                raise RuntimeError('Q2 prerequisite did not publish a verified result')
            from q2.pipeline import reusable
            if not reusable(final, read_json(final / 'complete.json')['fingerprint']):
                raise RuntimeError('Q2 prerequisite published incomplete or changed artifacts')
            plan = read_json(final / 'plan.json')
            result = read_json(final / 'result.json')
            record = dict(kind='generated_q2', source=str(final), source_algorithm='Q2-Repair-2.1',
                          source_plan_sha256=file_hash(final / 'plan.json'),
                          frozen_objective=True, source_result_sha256=file_hash(final / 'result.json'),
                          expected_objective=[result['makespan'], result['data_movement_bytes']['added_copy_bytes']],
                          generated_seconds=time.monotonic() - started)
    if set(plan) != {'node_to_subgraph', 'core_schedules'} or len(plan['core_schedules']) != cores:
        raise ValueError('invalid Q2 final plan format/core count')
    path = directory / 'q2_plan.json'
    write_json(path, plan)
    record.update(input_sha256=file_hash(graph_path), imported_plan_sha256=file_hash(path))
    write_json(directory / 'q2_source.json', record)
    return path, record
