"""Reuse real Step1/2/3 preparation without running the multicore simulator."""
from dataclasses import replace
import json
import pickle

from q2.contracts import LegalityRecord
from q2.evaluation import binding
from q2.official import digest
from q2.state import validate_state
from .contracts import ExecutionPreparation

PREPARATION_VERSION = 'Q2-Preparation-1.0'


def prepare_execution(service, state, *, check=lambda: None):
    """Return independent frozen evidence; callers handle illegal/unknown errors.

    Cached local tasks exclude submitted labels. Global links are checked for
    every distinct execution. Even hits validate the current state first.
    """
    from evaluation_validation import validate_execution
    from schedule_step2 import step2_spill_insertion, _build_extended_graph
    from schedule_step3 import prepare_step3_execution

    check()
    validate_state(service.g, state, preparation=service)
    settings, cache = service.g.settings, service.cache
    key = (state.environment_digest, PREPARATION_VERSION, state.execution_digest)
    cached = cache.get('execution', key)
    if cached is not None:
        return replace(cached, legality=replace(cached.legality, **binding(state)))
    bundle = service.prefix(state.plan)
    tasks, spill_bytes = {}, 0
    for core, prefix in bundle['cores'].items():
        check()
        local_key = (state.environment_digest, PREPARATION_VERSION,
                     digest(dict(graph=prefix['graph'], seq=prefix['seq'])))
        local = cache.get('prepared_core', local_key)
        if local is None:
            step2 = step2_spill_insertion(prefix['graph'], prefix['seq'], capacity=settings.capacity) if prefix['seq'] else dict(
                new_ops=[], new_tensors=[], new_edges=[], spill_records=[], seq_ext=[])
            check()
            expanded = _build_extended_graph(prefix['graph'], step2)
            prepared = prepare_step3_execution(expanded, capacity=settings.capacity, bandwidth=settings.bandwidth)
            spill = sum(row['size'] * (1 + int(row['spill_out_copies_data']))
                        for row in step2.get('spill_records', []))
            # Preserve label-independent preparation and spill ancestry separately.
            local = prepared, spill, step2.get('spill_records', [])
            cache.put('prepared_core', local_key, local)
            cache.stats['core_prepares'] += 1
        prepared, spill, _ = local
        # Labels do not belong in an execution-equivalence cache. Full labelled
        # official JSON is materialized separately for the actual current plan.
        prepared.update(task_id=core, core_id=core)
        tasks[core] = prepared
        spill_bytes += spill
    validate_execution(tasks, bundle['cross_links'])
    check()
    traffic = dict(original_graph_copy_bytes=bundle['original_copy_bytes'],
                   scheduled_copy_bytes=bundle['base_copy_bytes'] + spill_bytes,
                   added_copy_bytes=bundle['base_copy_bytes'] - bundle['original_copy_bytes'] + spill_bytes,
                   partition_added_copy_bytes=bundle['base_copy_bytes'] - bundle['original_copy_bytes'],
                   spill_added_copy_bytes=spill_bytes)
    answer = ExecutionPreparation(state.execution_digest, state.environment_digest,
        pickle.dumps(tasks, protocol=5), json.dumps(bundle['cross_links']), json.dumps(traffic),
        LegalityRecord(**binding(state), level='execution', status='VALID', validator_version=PREPARATION_VERSION))
    cache.put('execution', key, answer)
    cache.put('legality', (state.environment_digest, state.execution_digest), dict(status='VALID', reason=None))
    cache.stats['execution_prepares'] += 1
    return answer
