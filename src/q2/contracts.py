"""Q2 snapshots, separate from official submission JSON and search-engine policy."""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class ConstructionDraft:
    operation_core: tuple[int, ...]
    commit_order: tuple[int, ...]
    search_blocks: tuple[tuple[int, ...], ...]
    legacy_json: str
    family: str
    mode: str
    beta: int | None
    diagnostics_json: str
    constructor_version: str = 'Q2-Construction-2.0'

    @property
    def legacy_plan(self):
        return json.loads(self.legacy_json)

    @property
    def diagnostics(self):
        return json.loads(self.diagnostics_json)


@dataclass(frozen=True)
class EncodedCandidate:
    plan_json: str
    draft: ConstructionDraft
    encoding: str
    intended_order: tuple[int, ...] | None
    parent_version: str | None = None
    encoder_version: str = 'Q2-Encoding-2.0'

    @property
    def plan(self):
        return json.loads(self.plan_json)


@dataclass(frozen=True)
class BaselineResult:
    """Frozen serialized records; callers receive fresh containers, never live state."""
    plan_json: str
    official_json: str
    draft: ConstructionDraft
    search_blocks: tuple[tuple[int, ...], ...]
    execution_json: str
    candidates_json: str
    beta_json: str

    @property
    def plan(self):
        return json.loads(self.plan_json)

    @property
    def evaluation(self):
        return json.loads(self.official_json)

    @property
    def execution(self):
        return json.loads(self.execution_json)

    @classmethod
    def from_record(cls, record):
        draft = dict(record['draft'])
        for field in ('operation_core', 'commit_order'):
            draft[field] = tuple(draft[field])
        draft['search_blocks'] = tuple(map(tuple, draft['search_blocks']))
        values = dict(record, draft=ConstructionDraft(**draft),
                      search_blocks=tuple(map(tuple, record['search_blocks'])))
        return cls(**values)
