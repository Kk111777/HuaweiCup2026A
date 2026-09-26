"""Fixed hardware comes from the official config; these are search limits only."""
import math

DEFAULTS = dict(seconds=600.0, q2_seconds=600.0, verification_reserve=60.0,
                evaluation_timeout=120.0, max_candidates=240, batch_size=4,
                diagnostic_seeds=8, window_width=8, max_windows=8,
                migration_targets=3, joint_blocks=4, joint_candidates=24,
                cache_bytes=64 * 1024 * 1024, use_bounds=True)


def parameters(overrides=None):
    options = dict(DEFAULTS)
    if overrides:
        if set(overrides) - options.keys():
            raise ValueError('unknown Q3 search parameter')
        options.update(overrides)
    for name in ('seconds', 'q2_seconds', 'verification_reserve', 'evaluation_timeout'):
        value = options[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be finite and positive')
    if options['verification_reserve'] >= options['seconds']:
        raise ValueError('verification reserve must be smaller than the Q3 case budget')
    for name in ('max_candidates', 'batch_size', 'diagnostic_seeds', 'window_width',
                 'max_windows', 'migration_targets', 'joint_blocks', 'joint_candidates', 'cache_bytes'):
        if type(options[name]) is not int or options[name] < 1:
            raise ValueError(f'{name} must be a positive integer')
    if options['window_width'] > 8 or options['joint_blocks'] > 4:
        raise ValueError('window width <= 8 and joint blocks <= 4 required')
    for name, cap in (('diagnostic_seeds', 8), ('max_windows', 8), ('migration_targets', 3), ('joint_candidates', 24)):
        if options[name] > cap:
            raise ValueError(f'{name} must be <= {cap}')
    if type(options['use_bounds']) is not bool:
        raise ValueError('use_bounds must be a boolean')
    return options
