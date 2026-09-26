"""Search checkpoints are explicitly separate from verified CLI publications."""
from dataclasses import asdict, replace
from pathlib import Path
import json
import tempfile
from common.artifacts import write_json
from .contracts import ScheduleState, ScoreRecord, LegalityRecord, BaselineResult, ConstructionDraft, CandidateOrigin
from .evaluation import ScoredState, reconcile, exact_score, binding
from .official import read_json, file_hash, digest
from .state import build_state, validate_state


def write_search_snapshot(directory, incumbents):
    directory = Path(directory)
    if directory.exists(): raise FileExistsError(directory)
    validated = reconcile(incumbents.candidates)
    if validated != incumbents or validated.search is None:
        raise ValueError('snapshot must contain reconciled incumbents')
    directory.parent.mkdir(parents=True, exist_ok=True)
    status = 'SUCCESS_ESTIMATED' if not validated.search.score.exact else 'SUCCESS_EXACT_UNREPLAYED'
    with tempfile.TemporaryDirectory(dir=directory.parent, prefix='.q2-search-') as temporary:
        attempt = Path(temporary)
        write_json(attempt/'search_state.json', dict(schema_version=2, kind='q2_search_snapshot', status=status,
                   candidates=[asdict(row) for row in validated.candidates]))
        write_json(attempt/'search_manifest.json', dict(schema_version=2, kind='q2_search_snapshot', status=status,
                   artifacts_sha256={'search_state.json': file_hash(attempt/'search_state.json')}))
        attempt.rename(directory)
    return status


def load_search_snapshot(directory, graph, *, preparation=None):
    directory = Path(directory); manifest = read_json(directory/'search_manifest.json')
    if manifest.get('schema_version') != 2 or manifest.get('kind') != 'q2_search_snapshot':
        raise ValueError('search snapshot version mismatch')
    hashes = manifest.get('artifacts_sha256', {})
    if set(hashes) != {'search_state.json'} or file_hash(directory/'search_state.json') != hashes['search_state.json']:
        raise ValueError('search snapshot integrity mismatch')
    data = read_json(directory/'search_state.json')
    if (data.get('schema_version'), data.get('kind')) != (2, 'q2_search_snapshot'):
        raise ValueError('search state version mismatch')
    rows = []
    for row in data['candidates']:
        state = ScheduleState.from_record(row['state'])
        validate_state(graph, state, preparation=preparation)
        rows.append(ScoredState(state, ScoreRecord(**row['score']), LegalityRecord(**row['legality'])))
    result = reconcile(rows)
    if result.search is None: raise ValueError('empty snapshot')
    expected = 'SUCCESS_ESTIMATED' if not result.search.score.exact else 'SUCCESS_EXACT_UNREPLAYED'
    if data.get('status') != expected or manifest.get('status') != expected:
        raise ValueError('snapshot publication status mismatch')
    return result


def import_baseline_v2(directory, graph, graph_path):
    """Read-only explicit migration of a hash-verified 2.0 publication, never resume."""
    from .pipeline import reusable
    directory = Path(directory); graph_path = Path(graph_path)
    complete = read_json(directory/'complete.json')
    if not reusable(directory, complete.get('fingerprint')):
        raise ValueError('old publication integrity mismatch')
    provenance = read_json(directory/'provenance.json')
    identity = provenance['identity']
    if identity['algorithm']['algorithm_version'] != 'Q2-Repair-2.0':
        raise ValueError('explicit importer accepts only Q2 2.0')
    if provenance['fingerprint'] != digest(identity) or complete['fingerprint'] != provenance['fingerprint']:
        raise ValueError('old publication identity mismatch')
    if identity['graph_sha256'] != file_hash(graph_path) or read_json(graph_path) != graph.raw:
        raise ValueError('old input identity mismatch')
    if identity['official_sha256'] != graph.settings.hashes or identity['config_sha256'] != file_hash(graph.settings.config):
        raise ValueError('old official/config identity mismatch')
    record = read_json(directory/'baseline_state.json')
    if 'schema_version' in record: raise ValueError('not a 2.0 snapshot')
    plan = read_json(directory/'plan.json'); result = read_json(directory/'result.json')
    if json.loads(record['plan_json']) != plan or json.loads(record['official_json']) != result:
        raise ValueError('old snapshot differs from published plan/result')
    if len(plan['core_schedules']) != identity['num_cores']:
        raise ValueError('old core count mismatch')
    draft_record = dict(record['draft'])
    draft_record['construction_blocks_ops'] = draft_record.pop('search_blocks')
    draft = ConstructionDraft(**draft_record)
    run = read_json(directory/'run.json'); selected = run['selected_candidate']
    state = build_state(graph, plan, construction_blocks_ops=draft.construction_blocks_ops,
                        origin=CandidateOrigin(selected['family'], selected['mode'], selected['beta'], selected['encoding']))
    score = exact_score(state, (result['makespan'], result['data_movement_bytes']['added_copy_bytes']),
                        evidence_path=str(directory/'result.json'))
    score = replace(score, evaluator_version='Q2-Official-2.0-import', calibrated_from=complete['fingerprint'])
    return BaselineResult(state, score, LegalityRecord(**binding(state), level='execution', status='VALID'),
                          json.dumps(result), draft, record.get('execution_json'),
                          record.get('candidates_json', '[]'), record.get('beta_json', '{}'))
