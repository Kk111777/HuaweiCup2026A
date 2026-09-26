"""Q2 2.1 components; legacy algorithms require explicit q2.legacy imports."""
from .contracts import (BaselineResult, BlockState, CandidateOrigin, ConstructionDraft,
                        EncodedCandidate, LegalityRecord, ScheduleState, ScoreRecord)
from .state import build_state, derive_state, validate_state, candidate_from_state
from .preparation import PreparationCache, PreparationService
from .evaluation import ExecutionValidator, ScoredState, Incumbents, evaluate_approximate, reconcile
from .snapshots import write_search_snapshot, load_search_snapshot, import_baseline_v2
