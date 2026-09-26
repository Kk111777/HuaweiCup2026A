"""Compatibility entry point for the Q1 search coordinator.

Public symbols remain patchable by existing callers/tests. Each invocation
builds local bindings instead of changing modules or shared global registries.
Production orchestration calls scheduling.engine with its selected backend.
"""
from collections import defaultdict
import time
from .budget import SearchBudget
from ..improvement.incremental import IncrementalEvaluator, EvaluationPolicy
from ..improvement.operators import initial_candidates, local_candidates, _retain_states
from ..improvement.regions import rebuild_regions
from ..improvement.schedule import validate_plan, schedule, cheap_plan_proxy, CHEAP_PROXY_MODE, CHEAP_PROXY_NOTE
from ..plan import objective, plan_digest
from .features import recognize, plan_complexity, OfficialCostModel, control_profile
from .backend import get_problem_definition
from scheduling.engine import search as _search


def search(graph, cores, settings, params, seconds, services, *, seed=None,
           started=None, constructor=None, budget=None, problem=None):
    if problem is None:
        names = ('validate_plan', 'plan_digest', 'objective', 'IncrementalEvaluator',
                 'EvaluationPolicy', 'OfficialCostModel', 'recognize', 'plan_complexity',
                 'cheap_plan_proxy', 'CHEAP_PROXY_MODE', 'CHEAP_PROXY_NOTE',
                 'initial_candidates', 'local_candidates', 'rebuild_regions',
                 'schedule', '_retain_states', 'control_profile')
        problem = get_problem_definition({name: globals()[name] for name in names})
    return _search(graph, cores, settings, params, seconds, services, problem=problem,
                   seed=seed, started=started, constructor=constructor, budget=budget)
