"""真实 Step1 序列的闭区间无换出驻留证书，不是全局模拟峰值。"""
from dataclasses import dataclass
from math import lcm


def events(graph):
    local = {t['id']: (int(t['pos'] == 'UB'), t['size'])
             for t in graph['tensors'] if t['pos'] in ('L1', 'UB')}
    uses = {o['id']: set() for o in graph['ops']}
    for edge in graph['edges']:
        a, b = edge['source'], edge['target']
        if a in uses and b in local: uses[a].add(b)
        if b in uses and a in local: uses[b].add(a)
    return local, {o: tuple(sorted(ts)) for o, ts in uses.items()}


def scales(capacity):
    common = lcm(capacity['L1'], capacity['UB'])
    return (common//capacity['L1'], common//capacity['UB'])


@dataclass(frozen=True)
class Profile:
    peaks: tuple
    curve: tuple
    scaled: int
    peak_index: int | None
    first_overflow: int | None
    no_spill: bool

    def record(self):
        return dict(peak_l1=self.peaks[0], peak_ub=self.peaks[1], profile_peak_scaled=self.scaled,
                    peak_index=self.peak_index, first_overflow_index=self.first_overflow,
                    no_spill_certified=self.no_spill)


def profile(graph, seq, capacity):
    local, uses = events(graph)
    if len(seq) != len(uses) or set(seq) != set(uses): raise ValueError('sequence coverage differs')
    first, last = {}, {}
    for i, o in enumerate(seq):
        for t in uses[o]: first.setdefault(t, i); last[t] = i
    diff = [[0]*(len(seq)+1) for _ in range(2)]
    for t, start in first.items():
        pos, size = local[t]; diff[pos][start] += size; diff[pos][last[t]+1] -= size
    curve = [[], []]; live = [0, 0]; peaks = [0, 0]
    overflow = peak_at = None; scaled = 0
    scale = scales(capacity); caps = (capacity['L1'], capacity['UB'])
    for i in range(len(seq)):
        for p in (0, 1):
            live[p] += diff[p][i]; peaks[p] = max(peaks[p], live[p]); curve[p].append(live[p])
        value = max(live[p]*scale[p] for p in (0, 1))
        if peak_at is None or value > scaled: scaled = value; peak_at = i
        if overflow is None and any(live[p] > caps[p] for p in (0, 1)): overflow = i
    return Profile(tuple(peaks), tuple(map(tuple, curve)), scaled, peak_at, overflow, overflow is None)
