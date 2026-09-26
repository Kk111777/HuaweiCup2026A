"""One resumable per-core-count session; each batch uses one frozen parent."""
from collections import OrderedDict, Counter
from dataclasses import asdict, replace
import json
from time import perf_counter

from q2.construct import BudgetExpired
from q2.evaluation import better
from q2.official import digest
from .construction import Construction
from .contracts import OptimizationResult
from .events import ModelUnavailable
from .pool import StatePool, select_batch
from .proposals import (order_proposals, rebuild_proposals, feedback_proposals,
                        block_proposals, capacity_proposals, IncompatibleComputeOrder)
from .service import scheduling_errors


class SearchSession:
    def __init__(self, service, cores, parameters, budget, *, emit=lambda row: None, checkpoint=lambda session: None):
        self.service, self.g, self.cores, self.options = service, service.g, cores, parameters
        self.budget, self.emit, self.checkpoint = budget, emit, checkpoint
        self.constructor = Construction(self.g, cores, maximum=parameters['beta_maximum'], policy=parameters['beta_policy'])
        self.pool = StatePool(parameters['pool_size'])
        self.pending = OrderedDict()
        self.refined_seen, self.exhausted_parents = set(), set()
        self.no_new = Counter()
        self.stats = Counter()
        self.batches = 0
        self.calibration_cursor = 0
        self.stop_reason = None
        self.turn_deadline = float('inf')
        self.previous_best_signature = None
        self.windows = []
        from .protection import BaselineProtection
        self.protection = BaselineProtection(self) if parameters['baseline_guard'] else None

    def check(self):
        if self.budget.elapsed() >= self.budget.hard:
            raise BudgetExpired('case_hard_limit')
        if perf_counter() >= self.turn_deadline:
            raise BudgetExpired('case_turn_yield')

    def record(self, event, **values):
        self.emit(dict(event=event, cores=self.cores, batch=self.batches,
                       case_elapsed_seconds=self.budget.elapsed(), **values))

    def beta_feedback(self, state, score=None, status='OFFICIAL'):
        for stream, entry in list(self.pending.items()):
            if entry['state'].execution_digest == state.execution_digest:
                self.constructor.observe(entry['proposal'], score, status=status)
                del self.pending[stream]
                self.record('beta_feedback', stream=stream, beta=entry['proposal'].beta, status=status,
                            objective=[score.makespan, score.added_copy_bytes] if score else None)

    def _entry(self, proposal, parent=None):
        self.check()
        self.stats['proposals'] += 1
        info = proposal.parameters
        if 'blocks_by_core' in info:
            info = dict(info, blocks_digest=digest(info['blocks_by_core']))
            info.pop('blocks_by_core')
        header = dict(family=proposal.family, parent_signature=proposal.parent_signature,
                      parameters=info, region_ops=proposal.region_ops,
                      beta_stream=proposal.beta_stream, beta=proposal.beta)
        try:
            state = self.service.state(proposal, parent, check=self.check)
        except (ValueError, *scheduling_errors()) as error:
            self.record('candidate', status='INVALID_STRUCTURE', error=str(error), **header)
            if proposal.beta_stream is not None: self.constructor.observe(proposal, status='INVALID')
            self.stats['invalid'] += 1
            return None
        entry = dict(proposal=proposal, state=state, coarse=None)
        if proposal.beta_stream is not None:
            self.pending[proposal.beta_stream] = entry
        exact = self.service.cached_exact(state)
        if exact is not None:
            self.beta_feedback(state, exact, 'EQUIVALENT_OFFICIAL' if exact.source == 'equivalent_official' else 'CACHED_OFFICIAL')
        if state.state_signature in self.refined_seen:
            self.record('candidate', status='ALREADY_REFINED', state_signature=state.state_signature, **header)
            return None
        try:
            entry['coarse'] = self.service.model.evaluate(state, 'coarse', check=self.check)
        except ModelUnavailable as error:
            self.record('model_unavailable', level='coarse', state_signature=state.state_signature, error=str(error))
        coarse = entry['coarse']
        self.record('candidate', status='COARSE' if coarse else 'NEEDS_REFINED', **header,
                    state_signature=state.state_signature, plan_digest=state.plan_digest,
                    execution_digest=state.execution_digest, origin=asdict(state.origin),
                    coarse_cycles=coarse.raw_cycles if coarse else None,
                    coarse_corrected=coarse.score.makespan if coarse else None,
                    pressure=coarse.pressure if coarse else None,
                    lower_bound=coarse.lower_bound if coarse else None)
        return entry

    def initialize(self):
        """First execution-valid checkpoint, without invoking a complete baseline."""
        while self.pool.incumbents.search is None:
            proposal = self.constructor.next(check=self.check)
            if proposal is None:
                self.stop_reason = 'NO_VALID_INITIAL_STATE'; return
            entry = self._entry(proposal)
            if entry is None: continue
            answer = self._refine(entry)
            if answer is not None:
                row, trace = answer
                self.pool.update([row], [(row.state.state_signature, trace)])
                self.record('initial_checkpoint', state_signature=row.state.state_signature,
                            score=asdict(row.score))
                self.checkpoint(self)

    def _refine(self, entry):
        state = entry['state']
        self.check()
        try:
            row, trace = self.service.refined(state, check=self.check)
        except scheduling_errors() as error:
            self.stats['invalid'] += 1
            self.beta_feedback(state, status='INVALID')
            self.record('candidate', status='INVALID_EXECUTION', state_signature=state.state_signature, error=str(error))
            self.refined_seen.add(state.state_signature)
            return None
        except MemoryError:
            self.record('candidate', status='UNKNOWN_RESOURCE', state_signature=state.state_signature)
            return None
        exact = self.service.cached_exact(state)
        if exact is not None: row = replace(row, score=exact)
        self.refined_seen.add(state.state_signature)
        self.stats['refined_candidates'] += 1
        self.record('candidate', status='EXECUTION_VALID', state_signature=state.state_signature,
                    family=entry['proposal'].family, raw_refined_cycles=trace.raw_cycles,
                    score=asdict(row.score), spill_bytes=trace.spill_bytes,
                    lower_bound=trace.lower_bound)
        return row, trace

    def _baseline(self):
        # Only available streams, never wait for unofficial feedback or build a
        # second solver. Complete-plan/equivalent hits may unlock the next beta.
        for _ in range(26):
            proposal = self.constructor.next(check=self.check)
            if proposal is None: break
            yield proposal

    def _parent(self):
        self.pool.recalibrate(self.service.model)
        preferred = self.pool.incumbents.search
        if preferred is not None and preferred.state.state_signature not in self.exhausted_parents:
            return preferred
        return next((r for r in self.pool.rows.values() if r.state.state_signature not in self.exhausted_parents), None)

    def step_batch(self):
        if self.stop_reason is not None: return False
        if self.options['max_batches'] is not None and self.batches >= self.options['max_batches']:
            self.stop_reason = 'BATCH_LIMIT'; return False
        parent_row = self._parent()
        if parent_row is None and self.constructor.exhausted:
            self.stop_reason = 'PROPOSALS_EXHAUSTED'; return False
        if parent_row is None and self.pool.incumbents.search is not None:
            parent_row = self.pool.incumbents.search
        if parent_row is None:
            self.stop_reason = 'NO_VALID_INITIAL_STATE'; return False
        parent = parent_row.state
        trace = self.pool.traces.get(parent.state_signature)
        if trace is None: trace = self.service.model.evaluate(parent, check=self.check)
        before = self.pool.incumbents.search
        self.batches += 1
        model_version = self.service.model.calibration.version
        self.record('batch_begin', parent_signature=parent.state_signature, calibration_version=model_version)
        families = [('baseline', self._baseline())]
        if parent.state_signature not in self.exhausted_parents:
            families += [('order', order_proposals(self.g, parent, check=self.check)),
                         ('rebuild', rebuild_proposals(self.g, parent, check=self.check)),
                         ('migration', feedback_proposals(self.g, parent, trace, limit=self.options['migration_limit'], check=self.check)),
                         ('blocks', block_proposals(self.g, parent, trace, self.service.preparation,
                                                   limit=self.options['block_limit'], check=self.check)),
                         ('capacity', capacity_proposals(self.g, parent, trace, self.service.preparation, self.windows, check=self.check))]
        entries, signatures, accepted, traces = [], set(), [], []
        interrupted = None
        try:
            # Interleave generators; expensive later families cannot be starved
            # simply because one earlier generator fills all remaining time.
            active = list(families)
            while active:
                following = []
                for family, generator in active:
                    self.check()
                    try:
                        proposal = next(generator)
                    except StopIteration:
                        continue
                    except IncompatibleComputeOrder as error:
                        self.record('family_skipped', family=family, reason=str(error)); continue
                    entry = self._entry(proposal, parent if proposal.parent_signature else None)
                    if entry is not None and entry['state'].state_signature not in signatures:
                        signatures.add(entry['state'].state_signature); entries.append(entry)
                    following.append((family, generator))
                active = following
            selected = select_batch(entries, self.options['refine_per_batch'], self.options['family_minimum'], self.batches-1)
            selected_ids = {e['state'].state_signature for e in selected}
            for entry in entries:
                if entry['state'].state_signature not in selected_ids:
                    self.record('candidate', status='NOT_PROMOTED', family=entry['proposal'].family,
                                state_signature=entry['state'].state_signature, theoretical_pruning=False)
            for entry in selected:
                answer = self._refine(entry)
                if answer is not None:
                    row, tr = answer; accepted.append(row); traces.append((row.state.state_signature, tr))
        except (BudgetExpired, MemoryError) as error:
            interrupted = type(error).__name__ + ': ' + str(error)
        # A completed prefix of a batch remains usable, but no claim of batch
        # exhaustion is made. No accepted move changed the generating parent.
        self.pool.update(accepted, traces)
        if accepted:
            self.no_new[parent.state_signature] = 0
        else:
            self.no_new[parent.state_signature] += 1
            if self.no_new[parent.state_signature] >= 2: self.exhausted_parents.add(parent.state_signature)
        after = self.pool.incumbents.search
        if before is not None and after is not None:
            self.budget.observe({'makespan': before.score.makespan}, {'makespan': after.score.makespan},
                                dict(evaluation_source=after.score.source, seconds=0))
        self.record('batch_end', parent_signature=parent.state_signature, complete=interrupted is None,
                    interruption=interrupted, coarse_candidates=len(entries), valid_refined=len(accepted),
                    selected_signature=after.state.state_signature if after else None,
                    score=asdict(after.score) if after else None)
        self.checkpoint(self)
        if interrupted is None:
            self.calibrate(entries)
        if self.options['max_batches'] is not None and self.batches >= self.options['max_batches']:
            self.stop_reason = 'BATCH_LIMIT'
        elif all(k in self.exhausted_parents for k in self.pool.rows) and not self.constructor.components and not self.constructor.beta.queue:
            if not self.pending:
                self.stop_reason = 'PROPOSALS_EXHAUSTED'
            elif self.options['calibration'] == 'off':
                self.stop_reason = 'PROPOSALS_EXHAUSTED_BETA_PENDING'
        self.checkpoint(self)
        return True

    def calibrate(self, entries=()):
        if self.options['calibration'] == 'off': return
        requests = {}
        if self.pending:
            requests['beta'] = next(iter(self.pending.values()))['state']
        best = self.pool.incumbents.search
        if best is not None and not best.score.exact and self.batches % 2 == 0:
            if best.state.state_signature != self.previous_best_signature:
                requests['best'] = best.state
        coarse = {e['state'].execution_digest: e['coarse'] for e in entries if e['coarse'] is not None}
        differences = []
        for key, row in self.pool.rows.items():
            tr, cheap = self.pool.traces.get(key), coarse.get(row.state.execution_digest)
            if tr is not None and cheap is not None and not row.score.exact:
                difference = abs(tr.raw_cycles-cheap.raw_cycles)/max(1, tr.raw_cycles)
                if difference > self.options['disagreement_threshold']:
                    differences.append((difference, row.state))
        if differences: requests['disagreement'] = max(differences, key=lambda pair: pair[0])[1]
        if not requests: return
        categories = ('beta', 'best', 'disagreement')
        selected = None
        for offset in range(len(categories)):
            at = (self.calibration_cursor+offset) % len(categories)
            if categories[at] in requests:
                selected = categories[at]; self.calibration_cursor = (at+1) % len(categories); break
        state = requests[selected]
        estimate = self.service.model.costs.estimate('official', max(1, len(self.g.ids))**2)
        if not self.budget.can_start(estimate) or perf_counter()+estimate >= self.turn_deadline:
            self.record('calibration_deferred', reason='insufficient_time', source=selected); return
        timeout = min(self.budget.candidate_cap(), max(0., self.turn_deadline-perf_counter()))
        try:
            # Obtaining refined preparation also handles beta candidates that
            # were not selected in the batch's heuristic shortlist.
            row, trace = self.service.refined(state, check=self.check)
            measured, record = self.service.official(state, timeout, check=self.check)
        except scheduling_errors() as error:
            self.beta_feedback(state, status='INVALID')
            self.record('calibration_invalid', source=selected, error=str(error)); return
        except (BudgetExpired, MemoryError) as error:
            self.record('calibration_unknown', source=selected, reason=type(error).__name__); return
        if measured is None:
            self.budget.observe(None, None, dict(timed_out=record.get('status') == 'EVAL_TIMEOUT',
                timeout_seconds=timeout))
            self.record('calibration_unknown', source=selected, record=record)
            # Unknown is pending, not a synthetic numerical regression.
            if self.pending:
                for stream, entry in list(self.pending.items()):
                    if entry['state'].execution_digest == state.execution_digest: self.pending.move_to_end(stream)
            return
        self.refined_seen.add(state.state_signature)
        self.budget.observe(None, {'makespan': measured.score.makespan},
                            dict(seconds=record.get('wall_seconds', 0), evaluation_source=measured.score.source))
        self.beta_feedback(state, measured.score,
                           'EQUIVALENT_OFFICIAL' if measured.score.source == 'equivalent_official' else
                           'CACHED_OFFICIAL' if record.get('cached') else 'OFFICIAL')
        if not record.get('cached'):
            self.service.model.calibration.observe(trace, measured.score)
            cheap = coarse.get(state.execution_digest)
            if cheap is not None: self.service.model.calibration.observe(cheap, measured.score)
        self.pool.update([measured], [(state.state_signature, trace)])
        self.pool.recalibrate(self.service.model)
        self.previous_best_signature = best.state.state_signature if best is not None else None
        self.record('calibration', source=selected, state_signature=state.state_signature,
                    raw_refined_cycles=trace.raw_cycles, official_score=asdict(measured.score), record=record,
                    calibration=self.service.model.calibration.record())
        self.checkpoint(self)

    def result(self):
        self.pool.recalibrate(self.service.model)
        return OptimizationResult(self.pool.incumbents, self.stop_reason or 'CASE_BUDGET', self.batches,
            self.stop_reason == 'PROPOSALS_EXHAUSTED', json.dumps(self.constructor.record()),
            json.dumps(dict(self.stats, pool_evictions=self.pool.evictions)))
