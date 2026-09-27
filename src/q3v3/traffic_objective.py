"""Dimensionless time/traffic tradeoff with a hard relative-time guard.

The official problem has no weighted score. This is an explicit user-selected
search/delivery policy. Normalizers are fixed after evaluating all input seeds.
"""
from dataclasses import dataclass
from fractions import Fraction

from .objective import byte_hit_rate


def ddr_bytes(result):
    value = result['data_movement_bytes']['scheduled_copy_bytes'] - result['cache_stats']['hit_bytes']
    if value < 0:
        raise ValueError('negative DDR bytes')
    return value


def rational(value, name, maximum=1):
    try:
        value = Fraction(str(value))
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError(f'{name} must be finite') from exc
    if not 0 <= value <= maximum:
        raise ValueError(f'{name} must be in [0, {maximum}]')
    return value


@dataclass(frozen=True)
class TrafficPolicy:
    reference_cycles: int
    reference_ddr: int
    reference_added: int
    tolerance: Fraction = Fraction(1, 100)
    weight: Fraction = Fraction(1, 20)
    added_share: Fraction = Fraction(1, 4)

    @classmethod
    def from_result(cls, result, *, tolerance=.01, weight=.05, added_share=.25):
        m = result['data_movement_bytes']
        floor = max(1, m['original_graph_copy_bytes'])
        return cls(max(1, result['makespan']), max(floor, ddr_bytes(result)),
                   max(floor, m['added_copy_bytes']), rational(tolerance, 'cycle tolerance'),
                   rational(weight, 'traffic weight'), rational(added_share, 'added share'))

    @classmethod
    def from_record(cls, record):
        refs=[record[k] for k in ('reference_cycles','reference_ddr_bytes','reference_added_bytes')]
        if any(type(x) is not int or x<1 for x in refs):
            raise ValueError('invalid fixed objective normalizers')
        return cls(*refs,rational(record['cycle_tolerance'],'cycle tolerance'),
                   rational(record['traffic_weight'],'traffic weight'),
                   rational(record['added_share'],'added share'))

    def eligible(self, result, fastest_cycles):
        return result['makespan'] <= (1 + self.tolerance) * fastest_cycles

    def score(self, result):
        traffic = ((1-self.added_share) * Fraction(ddr_bytes(result), self.reference_ddr)
                   + self.added_share * Fraction(result['data_movement_bytes']['added_copy_bytes'], self.reference_added))
        return Fraction(result['makespan'], self.reference_cycles) + self.weight * traffic

    def key(self, result):
        return (self.score(result), result['makespan'], ddr_bytes(result),
                result['data_movement_bytes']['added_copy_bytes'], -byte_hit_rate(result))

    def record(self, result=None, fastest_cycles=None):
        out = dict(policy='cycles-traffic', reference_cycles=self.reference_cycles,
            reference_ddr_bytes=self.reference_ddr, reference_added_bytes=self.reference_added,
            cycle_tolerance=str(self.tolerance), traffic_weight=str(self.weight),
            added_share=str(self.added_share),
            formula='T/Tref + weight*((1-added_share)*DDR/Dref + added_share*added_COPY/Aref)',
            note='Fixed seed normalizers; hard cap tracks fastest evaluated feasible plan; not an official score.')
        if result is not None:
            score = self.score(result)
            out.update(score=float(score), score_numerator=score.numerator, score_denominator=score.denominator)
        if fastest_cycles is not None:
            out['fastest_cycles'] = fastest_cycles
            out['maximum_cycles'] = int((1+self.tolerance)*fastest_cycles)
        return out


def pareto(items):
    """Safe global archive pruning; time/DDR/added COPY all matter to selection."""
    def vector(item):
        r = item['result']
        return r['makespan'], ddr_bytes(r), r['data_movement_bytes']['added_copy_bytes']
    out = []
    for item in sorted(items, key=lambda x:(vector(x), -byte_hit_rate(x['result']), x['digest'])):
        v = vector(item)
        if any(all(a <= b for a,b in zip(vector(old),v)) for old in out):
            continue
        out.append(item)
    return out


def select(items, policy):
    fastest = min(items, key=lambda x:(x['result']['makespan'], ddr_bytes(x['result']), x['digest']))
    eligible = [x for x in items if policy.eligible(x['result'],fastest['result']['makespan'])]
    return min(eligible,key=lambda x:(policy.key(x['result']),x['digest'])), fastest


def search_beam(items, policy, limit=6):
    """Keep low-traffic intermediates beyond the final time guard for exploration."""
    if not items:
        return []
    best, fastest = select(items,policy)
    chosen = []
    ranked = [best,fastest,
        min(items,key=lambda x:(x['result']['data_movement_bytes']['spill_added_copy_bytes'],policy.key(x['result']))),
        min(items,key=lambda x:(ddr_bytes(x['result']),policy.key(x['result'])))]
    ranked += sorted(items,key=lambda x:(policy.key(x['result']),x['digest']))
    for x in ranked:
        if all(x['digest'] != y['digest'] for y in chosen):
            chosen.append(x)
        if len(chosen) == limit:
            break
    return chosen
