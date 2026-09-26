"""Explicit backend allowlist, with complete definitions injectable for tests."""
from importlib import import_module
from types import MappingProxyType

from .contracts import ProblemDefinition


# A configuration string cannot import arbitrary Python or select an unbuilt
# problem. Further official problems require a real adapter and an explicit entry.
BACKENDS = MappingProxyType({'q1': ('q1.unified.backend', 'get_problem_definition')})


def resolve_problem(value='q1'):
    if isinstance(value, ProblemDefinition):
        return value
    if not isinstance(value, str) or value not in BACKENDS:
        raise ValueError(f'unknown problem backend {value!r}; available: {", ".join(BACKENDS)}')
    module, factory = BACKENDS[value]
    definition = getattr(import_module(module), factory)()
    if not isinstance(definition, ProblemDefinition) or definition.id != value:
        raise TypeError(f'backend {value!r} did not return its complete ProblemDefinition')
    return definition


get_problem = resolve_problem
