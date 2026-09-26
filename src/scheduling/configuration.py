"""Explicit overrides for backend-controlled search parameters.

Legacy configurations keep their automatic structural profile. An opt-in
structure_overrides mapping takes precedence over that profile; it cannot
silently select a different execution model or alter hardware parameters.
"""
from copy import deepcopy


def apply_structure_overrides(parameters, controlled_parameters):
    overrides = parameters.get('structure_overrides', {})
    if not isinstance(overrides, dict):
        raise TypeError('structure_overrides must be an object')
    unknown = set(overrides) - set(controlled_parameters)
    if unknown:
        raise ValueError(f'unknown structure override keys: {sorted(unknown)}')
    effective = dict(parameters)
    for name, value in overrides.items():
        reference = parameters[name]
        if type(value) is not type(reference):
            raise TypeError(f'structure_overrides.{name} must be {type(reference).__name__}')
        if isinstance(value, int) and value < 0:
            raise ValueError(f'structure_overrides.{name} must be nonnegative')
        if isinstance(value, list) and any(type(v) is not int or v <= 0 for v in value):
            raise ValueError(f'structure_overrides.{name} must contain positive integers')
        effective[name] = deepcopy(value)
    return effective
