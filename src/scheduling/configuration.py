"""Explicit overrides for backend-controlled search parameters.

Legacy configurations keep their automatic structural profile. An opt-in
structure_overrides mapping takes precedence over that profile; it cannot
silently select a different execution model or alter hardware parameters.
"""
from copy import deepcopy
import math


def intermediate_artifacts(parameters):
    options = parameters.get('artifacts', {})
    if not isinstance(options, dict) or set(options) - {'intermediate'}:
        raise ValueError('artifacts must contain only intermediate')
    enabled = options.get('intermediate', True)
    if type(enabled) is not bool:
        raise TypeError('artifacts.intermediate must be a boolean')
    return enabled


def resolve_time_budget(parameters, seconds=None):
    """Explicit seconds overrides the target and scales the configured ratios."""
    options = parameters.get('time_budget', {})
    allowed = {'target_seconds', 'extension_seconds', 'hard_limit_seconds'}
    if not isinstance(options, dict) or set(options) - allowed:
        raise ValueError('unknown time_budget field')
    target = options.get('target_seconds', 600)
    def positive(value):
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    if not positive(target):
        raise ValueError('time_budget.target_seconds must be finite and positive')
    extension = options.get('extension_seconds', target * 1.5)
    hard = options.get('hard_limit_seconds', target * 2)
    if not positive(extension) or not positive(hard) or not target <= extension <= hard:
        raise ValueError('time_budget requires 0 < target_seconds <= extension_seconds <= hard_limit_seconds')
    if seconds is not None:
        if not positive(seconds):
            raise ValueError('seconds must be finite and positive')
        factor = seconds / target
        target, extension, hard = seconds, extension * factor, hard * factor
        if not positive(extension) or not positive(hard):
            raise ValueError('scaled time budget must be finite and positive')
    return dict(target_seconds=target, extension_seconds=extension, hard_limit_seconds=hard)


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
