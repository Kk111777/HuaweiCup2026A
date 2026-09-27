"""Read-only, process-isolated access to the current Q1 graph-only seed API.

No Q1 score, single-core baseline or Q1 search result is used as a feature.
The returned plans are unscored proposals and require the Q3 official evaluator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
_LOCAL_Q1_SOURCE = ROOT / "integration" / "q1-structure-modular-publish" / "src"
DEFAULT_Q1_SOURCE = (_LOCAL_Q1_SOURCE if (_LOCAL_Q1_SOURCE / "scheduling/registry.py").is_file()
                     else ROOT / "src")
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT / 'src'))
from q3v3.runtime import run_command


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_paths(source_root: Path) -> tuple[Path, Path]:
    source = Path(source_root).resolve()
    if (source / "src" / "q1").is_dir() or (source / "src" / "scheduling").is_dir():
        source /= "src"
    if not ((source / 'scheduling/registry.py').is_file() or (source / 'q1/unified/seed.py').is_file()):
        raise ValueError(f"Q1 source lacks public scheduling registry or legacy seed API: {source}")
    repository = source.parent
    if not (repository / "official" / "code").is_dir():
        raise ValueError(f"Q1 source lacks its official/code directory: {repository}")
    return source, repository


def _source_files(source, repository):
    paths = sorted(source.rglob('*.py'))
    paths += sorted((repository / 'official/code').glob('*.py'))
    paths += sorted((repository / 'configs').rglob('*.json'))
    config = repository / 'official/data/config.txt'
    if config.is_file():
        paths.append(config)
    return paths


def _source_fingerprint(source: Path, repository: Path) -> dict:
    paths = _source_files(source, repository)
    files = {p.relative_to(repository).as_posix(): _sha256(p) for p in paths}
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(payload).hexdigest(), "files": files}


def snapshot_source(source_root, output_dir):
    """Freeze a coherent source/config copy once before a case-parallel run.

    The live Q1 tree remains editable. Only source/config files are captured;
    old results, caches and input datasets are not copied. Verify the live file
    set and all bytes twice before publishing the local snapshot.
    """
    source, repository = _source_paths(Path(source_root))
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f'snapshot directory already exists: {output}')
    started = time.perf_counter()
    for attempt in range(3):
        try:
            paths = _source_files(source, repository)
            captured = {p.relative_to(repository).as_posix(): p.read_bytes() for p in paths}
            files = {p: hashlib.sha256(data).hexdigest() for p, data in captured.items()}
            check = _source_fingerprint(source, repository)
            if files == check['files']:
                break
        except FileNotFoundError:
            pass  # A concurrent rename: retry a complete capture, never mix it.
    else:
        raise RuntimeError('Q1 source kept changing during snapshot capture; no mixed snapshot written')
    for name, data in captured.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    captured_source = output / source.relative_to(repository)
    if _source_fingerprint(captured_source, output) != check:
        raise RuntimeError('Q1 snapshot verification failed')
    meta = dict(original_source_root=str(source), snapshot_source_root=str(captured_source),
                source_sha256=check['sha256'], files=files, file_count=len(files),
                capture_seconds=time.perf_counter()-started,
                purpose='Read-only input snapshot; user Q1 edits do not change an active Q3 batch.')
    _write_json(output/'snapshot.json', meta)
    return meta


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def export_seeds(graph_path, config_path, cores: int, source_root: Path,
                 output_dir: Path, timeout=60) -> dict:
    """Export unscored Q1 graph-only seeds without importing Q1 in this process.

    ``source_root`` accepts the Q1 repository or its ``src`` directory. The
    output directory receives a request, worker log and ``q1_seeds.json``.
    A timeout or a changing source/input/config fails explicitly; callers may
    retain another verified seed but must not treat a stale export as success.
    """
    if type(cores) is not int or cores not in (2, 3, 4, 5):
        raise ValueError("Q1 structural seed supports 2, 3, 4 or 5 cores")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    source, _ = _source_paths(source_root)
    graph, config = Path(graph_path).resolve(), Path(config_path).resolve()
    inputs = {"input_sha256": _sha256(graph), "config_sha256": _sha256(config)}
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    request_path, result_path = output / "q1_seed_request.json", output / "q1_seeds.json"
    if result_path.exists() or request_path.exists():
        raise FileExistsError(f"Q1 seed export already exists: {output}")
    _write_json(request_path, dict(graph_path=str(graph), config_path=str(config),
                                 cores=cores, source_root=str(source), **inputs))
    python = sys._base_executable if os.name == 'nt' else sys.executable
    command = [python, "-I", "-B", str(Path(__file__).resolve()),
               "_worker", str(request_path), str(result_path)]
    log_path = output / "q1_seed_worker.log"
    try:
        completed = run_command(command, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        log_path.write_text(completed.stdout + completed.stderr, encoding='utf-8')
    except subprocess.TimeoutExpired as error:
        log_path.write_text(str(error), encoding='utf-8')
        raise TimeoutError(f"Q1 graph-only seed export exceeded {timeout}s; {log_path}") from error
    if completed.returncode or not result_path.is_file():
        details = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
        raise RuntimeError(f"Q1 graph-only seed export failed ({completed.returncode}): {details}")
    answer = json.loads(result_path.read_text(encoding="utf-8"))
    if answer.get("contract_version") != 1 or not answer.get("plans"):
        raise ValueError("invalid Q1 seed export contract")
    for key, value in inputs.items():
        if answer["provenance"].get(key) != value:
            raise ValueError(f"Q1 seed export changed {key}")
    if inputs != {"input_sha256": _sha256(graph), "config_sha256": _sha256(config)}:
        raise ValueError("input/config changed during Q1 seed export")
    return answer


def _bindings(source):
    if (source/'scheduling/registry.py').is_file():
        from scheduling.registry import resolve_problem
        definition = resolve_problem('q1')
        if definition.id != 'q1':
            raise ValueError('requested Q1 provider returned another problem')
        # Do not invoke the public Q1 search, proxy, or evaluator. These are the
        # only graph-only contracts needed by Q3; internal moves stay private.
        return SimpleNamespace(read_settings=definition.execution.read_settings,
            settings_dict=definition.execution.settings_dict, create_model=definition.execution.create_model,
            recognize=definition.prediction.recognize, seed=definition.proposals.seed,
            validate=definition.semantics.validate, cores=definition.supported_cores,
            score_functions=(definition.execution.evaluate, definition.execution.preanalyze),
            api='scheduling.registry.resolve_problem(q1) / ProblemDefinition',
            api_mode='problem_definition_v1', backend_version=definition.version)
    # Compatibility with a pre-refactor checkout is deliberately isolated here.
    # A broken public provider must fail; never hide it with a legacy retry.
    from q1 import official
    from q1.model import Graph
    from q1.plan import validate_plan
    from q1.unified.features import recognize
    from q1.unified.seed import structural_plan
    return SimpleNamespace(read_settings=official.Settings.read,
        settings_dict=lambda s: dict(capacity=s.capacity, bandwidth=s.bandwidth,
            task_same_core_wait_cycles=s.same_core_wait, task_cross_core_wait_cycles=s.cross_core_wait),
        create_model=Graph, recognize=recognize, seed=structural_plan, validate=validate_plan,
        cores=(2,3,4,5), score_functions=(official.evaluate, official.preanalyze, official.singlecore),
        api='q1.unified.features.recognize + q1.unified.seed.structural_plan',
        api_mode='legacy_graph_seed', backend_version='legacy')


def _worker(request_path: Path, result_path: Path) -> None:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    source, repository = _source_paths(Path(request["source_root"]))
    before = _source_fingerprint(source, repository)
    graph_path, config_path = Path(request["graph_path"]), Path(request["config_path"])
    if (_sha256(graph_path) != request["input_sha256"] or
            _sha256(config_path) != request["config_sha256"]):
        raise ValueError("input/config changed before Q1 seed export")
    # This module is launched as a file under isolated Python, never through
    # root's q1 package. Q1 may add only its own official/code import directory.
    sys.path.insert(0, str(source))
    bindings = _bindings(source)
    if request['cores'] not in bindings.cores:
        raise ValueError('Q1 provider does not support the requested core count')
    blocked_calls = []
    codes = {getattr(f, '__code__', None) for f in bindings.score_functions} - {None}
    score_names = {'evaluate', 'singlecore', 'preanalyze', 'evaluate_singlecore',
                   'evaluate_scene_a', 'evaluate_scene_b', 'evaluate_problem_3'}

    def forbid_scoring(frame, event, arg):
        if event == 'call' and (frame.f_code in codes or frame.f_code.co_name in score_names):
            blocked_calls.append(frame.f_code.co_name)
            raise RuntimeError('Q1 graph-only bridge forbids baseline, preanalysis and scoring')

    raw = json.loads(graph_path.read_text(encoding="utf-8-sig"))
    previous_profile = sys.getprofile()
    try:
        sys.setprofile(forbid_scoring)
        settings = bindings.read_settings(config_path)
        model = bindings.create_model(raw)
        features = bindings.recognize(model, bindings.settings_dict(settings))
        plan, metadata = bindings.seed(model, request['cores'], settings)
        bindings.validate(model, plan, request['cores'])
    finally:
        sys.setprofile(previous_profile)
    if blocked_calls:
        raise RuntimeError("Q1 seed attempted forbidden official scoring")
    # Cheap, graph-only fragments for the Q3 backend's own assignment/order.
    # Contiguous topological cuts preserve the quotient DAG. Their viability
    # and whole-core memory behavior are still decided by official Q3 replay.
    block_candidates, seen = [], set()
    for multiplier in (1, 2):
        count = min(len(model.order), request["cores"] * multiplier)
        if not count:
            continue
        blocks = [model.order[i * len(model.order) // count:
                              (i + 1) * len(model.order) // count] for i in range(count)]
        identity = tuple(map(tuple, blocks))
        if identity not in seen:
            seen.add(identity)
            block_candidates.append({"name": f"graph_topological_{multiplier}x",
                                     "blocks": blocks, "score_source": "unscored"})
    after = _source_fingerprint(source, repository)
    if before != after:
        raise RuntimeError("Q1 source changed during export; retry against a stable source")
    if (_sha256(graph_path) != request["input_sha256"] or
            _sha256(config_path) != request["config_sha256"]):
        raise ValueError("input/config changed during Q1 seed export")
    answer = dict(contract_version=1,
        plans=[dict(name="q1_graph_structural", plan=plan, metadata=metadata,
                    score_source="unscored")],
        block_candidates=block_candidates, features=features,
        provenance=dict(q1_source_root=str(source), q1_source_sha256=before["sha256"],
            q1_source_files=before["files"], input_sha256=request["input_sha256"],
            config_sha256=request["config_sha256"], cores=request["cores"],
            source_api=bindings.api, api_mode=bindings.api_mode, backend_version=bindings.backend_version,
            baseline_used=False, q1_scoring_calls=len(blocked_calls),
            process_isolated=True,
            q1_module=str(Path(sys.modules['q1'].__file__).resolve()) if 'q1' in sys.modules else None,
            python_version=sys.version.split()[0],
            note="Q1 plans are unscored proposals; Q3 official evaluation is required."))
    _write_json(result_path, answer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["_worker"])
    parser.add_argument("request", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    _worker(args.request, args.result)
