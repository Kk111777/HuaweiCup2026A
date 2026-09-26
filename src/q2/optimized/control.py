"""Explicit optimized-model defaults and deterministic parameter validation."""
from pathlib import Path
import json
import math
from . import VERSION


def default_parameters():
    path = Path(__file__).resolve().parents[3]/'configs/q2/optimized.json'
    return json.loads(path.read_text(encoding='utf-8'))


def parameters(value=None):
    result = default_parameters()
    if value is not None:
        if not isinstance(value, dict) or set(value)-set(result): raise ValueError('unknown optimized parameters')
        result.update(value)
    if result['algorithm_version'] != VERSION: raise ValueError('unknown optimized algorithm version')
    if type(result['baseline_guard']) is not bool: raise ValueError('baseline_guard must be boolean')
    for key, low, high in [('pool_size', 2, 64), ('refine_per_batch', 1, 128), ('family_minimum', 1, 12),
                           ('migration_limit', 1, 96), ('block_limit', 1, 96), ('beta_maximum', 1, 4),
                           ('cache_bytes', 0, 2**31)]:
        if type(result[key]) is not int or not low <= result[key] <= high: raise ValueError(f'invalid {key}')
    if result['max_batches'] is not None and (type(result['max_batches']) is not int or result['max_batches'] < 1):
        raise ValueError('max_batches must be null or a positive integer')
    if result['beta_policy'] not in ('adaptive', 'fixed', 'grid'): raise ValueError('invalid beta policy')
    if result['calibration'] not in ('periodic', 'off'): raise ValueError('invalid calibration policy')
    if result['output_policy'] not in ('search', 'best-exact'): raise ValueError('invalid output policy')
    v = result['disagreement_threshold']
    if type(v) not in (float, int) or not math.isfinite(v) or v < 0: raise ValueError('invalid disagreement threshold')
    return result
