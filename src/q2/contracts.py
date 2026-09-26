"""Immutable state, evidence and origin contracts; no search policy here."""
from dataclasses import dataclass
import json
import math

STATE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ConstructionDraft:
    operation_core: tuple[int, ...]
    commit_order: tuple[int, ...]
    construction_blocks_ops: tuple[tuple[int, ...], ...]
    legacy_json: str
    family: str
    mode: str
    beta: int | None
    diagnostics_json: str
    constructor_version: str = 'Q2-Construction-2.1'

    def __post_init__(self):
        for name in ('operation_core', 'commit_order'):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, 'construction_blocks_ops', tuple(map(tuple, self.construction_blocks_ops)))

    @property
    def legacy_plan(self):
        return json.loads(self.legacy_json)

    @property
    def diagnostics(self):
        return json.loads(self.diagnostics_json)


@dataclass(frozen=True)
class CandidateOrigin:
    family: str = 'external'
    mode: str = 'external'
    beta: int | None = None
    encoding: str = 'external'


@dataclass(frozen=True)
class BlockState:
    block_id: str
    ops: tuple[int, ...]  # dense computation indices, never sgids or original IDs
    core: int
    sgids: tuple[int, ...]
    position: int
    origin_block_ids: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ('ops', 'sgids', 'origin_block_ids'):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class ScheduleState:
    plan_json: str
    core_of_op: tuple[int, ...]
    compute_orders_by_core: tuple[tuple[int, ...], ...]
    realized_blocks_by_core: tuple[tuple[BlockState, ...], ...]
    environment_digest: str
    plan_digest: str
    state_signature: str
    execution_digest: str
    origin: CandidateOrigin = CandidateOrigin()
    parent_signature: str | None = None
    schema_version: int = STATE_SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != STATE_SCHEMA_VERSION: raise ValueError('state schema version mismatch')
        object.__setattr__(self, 'core_of_op', tuple(self.core_of_op))
        object.__setattr__(self, 'compute_orders_by_core', tuple(map(tuple, self.compute_orders_by_core)))
        object.__setattr__(self, 'realized_blocks_by_core', tuple(map(tuple, self.realized_blocks_by_core)))
        if any(not isinstance(b, BlockState) for row in self.realized_blocks_by_core for b in row):
            raise TypeError('typed BlockState required')

    @property
    def plan(self): return json.loads(self.plan_json)

    @classmethod
    def from_record(cls, record):
        value = dict(record, origin=CandidateOrigin(**record['origin']))
        value['realized_blocks_by_core'] = tuple(tuple(BlockState(**b) for b in row)
                                                 for row in record['realized_blocks_by_core'])
        return cls(**value)


@dataclass(frozen=True)
class EncodedCandidate:
    """An unprepared proposal may carry a draft, never authoritative current state."""
    plan_json: str
    draft: ConstructionDraft | None
    encoding: str
    intended_order: tuple[int, ...] | None
    parent_version: str | None = None
    encoder_version: str = 'Q2-Encoding-2.1'
    state: ScheduleState | None = None

    def __post_init__(self):
        if self.intended_order is not None:
            object.__setattr__(self, 'intended_order', tuple(self.intended_order))

    @property
    def plan(self):
        return json.loads(self.plan_json)


@dataclass(frozen=True)
class LegalityRecord:
    state_signature: str
    plan_digest: str
    execution_digest: str
    environment_digest: str
    level: str  # structural / execution
    status: str  # VALID / INVALID / UNKNOWN
    reason: str | None = None
    validator_version: str = 'Q2-Legality-2.1'

    def __post_init__(self):
        if self.level not in ('structural', 'execution') or self.status not in ('VALID', 'INVALID', 'UNKNOWN'):
            raise ValueError('invalid legality record')


@dataclass(frozen=True)
class ScoreRecord:
    state_signature: str
    plan_digest: str
    execution_digest: str
    environment_digest: str
    makespan: int | float
    added_copy_bytes: int | None
    source: str
    exact: bool
    evaluator_version: str
    calibrated_from: str | None = None
    evidence_plan_digest: str | None = None
    evidence_path: str | None = None

    def __post_init__(self):
        if self.source not in ('approximate', 'equivalent_official', 'official'):
            raise ValueError('unknown score source')
        if type(self.exact) is not bool or self.exact != (self.source != 'approximate'):
            raise ValueError('score source/exact mismatch')
        if type(self.makespan) not in (int, float) or not math.isfinite(self.makespan) or self.makespan < 0:
            raise ValueError('nonnegative finite makespan required')
        if self.added_copy_bytes is not None and (type(self.added_copy_bytes) is not int or self.added_copy_bytes < 0):
            raise ValueError('invalid added bytes')
        if self.exact and (type(self.makespan) is not int or self.added_copy_bytes is None):
            raise ValueError('exact scores require integer cycles and known added bytes')
        if not self.evaluator_version: raise ValueError('evaluator version required')
        if self.source == 'equivalent_official' and not self.evidence_plan_digest:
            raise ValueError('equivalence source evidence required')


@dataclass(frozen=True)
class BaselineResult:
    """A reusable state with optional official evidence, not a publication flag."""
    state: ScheduleState
    score: ScoreRecord
    legality: LegalityRecord
    official_json: str | None = None
    draft: ConstructionDraft | None = None
    execution_json: str | None = None
    candidates_json: str = '[]'
    beta_json: str = '{}'
    schema_version: int = STATE_SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != STATE_SCHEMA_VERSION: raise ValueError('result schema version mismatch')
        for evidence in (self.score, self.legality):
            for name in ('state_signature', 'plan_digest', 'execution_digest', 'environment_digest'):
                if getattr(evidence, name) != getattr(self.state, name): raise ValueError('evidence/state mismatch')
        if self.legality.level != 'execution' or self.legality.status != 'VALID':
            raise ValueError('result requires confirmed execution legality')
        if self.official_json is not None:
            if self.score.source != 'official': raise ValueError('full official evidence must belong to this plan')
            result = json.loads(self.official_json)
            if (result['makespan'], result['data_movement_bytes']['added_copy_bytes']) != (self.score.makespan, self.score.added_copy_bytes):
                raise ValueError('official objective differs from score')

    @property
    def plan(self):
        return self.state.plan

    @property
    def evaluation(self):
        return json.loads(self.official_json) if self.official_json is not None else None

    @property
    def execution(self):
        return json.loads(self.execution_json) if self.execution_json is not None else None

    @property
    def realized_blocks_by_core(self): return self.state.realized_blocks_by_core

    @classmethod
    def from_record(cls, record):
        if record.get('schema_version') != STATE_SCHEMA_VERSION:
            raise ValueError('unsupported result version; use explicit Q2 2.0 importer')
        values = dict(record, state=ScheduleState.from_record(record['state']),
                      score=ScoreRecord(**record['score']), legality=LegalityRecord(**record['legality']))
        if values.get('draft') is not None: values['draft'] = ConstructionDraft(**values['draft'])
        return cls(**values)
