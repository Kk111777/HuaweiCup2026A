"""Exact, explicit search objectives; these are not official contest scores."""
from fractions import Fraction


DEFAULT_POLICY = 'cycles-hit'
POLICIES = (DEFAULT_POLICY, 'cycles-copy', 'cycles-traffic')


def byte_hit_rate(result):
    cache = result['cache_stats']
    hit, miss = cache['hit_bytes'], cache['miss_bytes']
    if hit < 0 or miss < 0:
        raise ValueError('negative cache traffic')
    return Fraction(hit, hit + miss) if hit + miss else Fraction(0)


def key(result, policy=DEFAULT_POLICY):
    """One extra cycle always loses, whatever the cache hit rate.

    Exact rational comparison avoids rounding away small secondary gains.
    Zero reads use the same zero hit-rate convention as official reporting.
    DDR and logical added COPY only break ties in both requested objectives.
    """
    movement = result['data_movement_bytes']
    if policy == 'cycles-copy':
        return result['makespan'], movement['added_copy_bytes']
    if policy != DEFAULT_POLICY:
        raise ValueError(f'unknown objective: {policy}')
    ddr = movement['scheduled_copy_bytes'] - result['cache_stats']['hit_bytes']
    if ddr < 0:
        raise ValueError('negative physical DDR traffic')
    return result['makespan'], -byte_hit_rate(result), ddr, movement['added_copy_bytes']


def describe(policy=DEFAULT_POLICY):
    if policy not in POLICIES:
        raise ValueError(f'unknown objective: {policy}')
    if policy == 'cycles-traffic':
        return ['guard_cycles_vs_fastest', 'min_normalized_time_and_traffic', 'min_cycles',
                'min_physical_DDR_bytes', 'min_added_COPY_bytes', 'max_L2_byte_hit_rate']
    return (['min_cycles', 'max_L2_byte_hit_rate', 'min_physical_DDR_bytes', 'min_added_COPY_bytes']
            if policy == DEFAULT_POLICY else ['min_cycles', 'min_added_COPY_bytes'])


def record(result, policy=DEFAULT_POLICY):
    """JSON-safe objective evidence; never serialize Fraction as a float key."""
    rate = byte_hit_rate(result)
    return dict(policy=policy, priority=describe(policy), cycles=result['makespan'],
                byte_hit_numerator=rate.numerator, byte_hit_denominator=rate.denominator,
                physical_ddr_bytes=result['data_movement_bytes']['scheduled_copy_bytes']
                    - result['cache_stats']['hit_bytes'],
                added_copy_bytes=result['data_movement_bytes']['added_copy_bytes'])


def improvement(before, after, policy=DEFAULT_POLICY):
    if key(after, policy) >= key(before, policy):
        return None
    if after['makespan'] < before['makespan']:
        return 'cycles'
    if policy == DEFAULT_POLICY and byte_hit_rate(after) > byte_hit_rate(before):
        return 'byte_hit_rate_at_equal_cycles'
    return 'traffic_at_equal_requested_objectives'
