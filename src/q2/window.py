"""规范第 11–12 节：外部路径约束、动态 COPY_IN 桶和标量峰值子集 DP。"""
from collections import Counter
from dataclasses import dataclass
import heapq
from .construct import BudgetExpired
from .memory import events, scales, profile
from .official import Q2Error


def precedence(view, core, left, right):
    window = view['core_orders'][core][left:right]
    bit = {j: 1 << i for i, j in enumerate(window)}
    succ = {j: set() for j in view['subgraph_ids']}
    # 数据边和顺序边分别建立，删除顺序理由永不删除数据理由。
    for a, b in view['dependency_pairs']: succ[a].add(b)
    for k, order in view['core_orders'].items():
        for a, b in zip(order, order[1:]):
            if k != core or (a not in bit and b not in bit): succ[a].add(b)
    order = view['core_orders'][core]
    for j in window:
        if left: succ[order[left-1]].add(j)
        if right < len(order): succ[j].add(order[right])
    degree = dict.fromkeys(succ, 0); reach = dict.fromkeys(succ, 0)
    for ss in succ.values():
        for w in ss: degree[w] += 1
    ready = [j for j, d in degree.items() if not d]; heapq.heapify(ready)
    visited = 0
    while ready:
        j = heapq.heappop(ready); visited += 1
        for w in sorted(succ[j]):
            reach[w] |= reach[j] | bit.get(j, 0)
            degree[w] -= 1
            if not degree[w]: heapq.heappush(ready, w)
    if visited != len(succ): raise AssertionError('window safe-domain graph contains a cycle')
    result = tuple(reach[j] for j in window)
    for i, mask in enumerate(result):
        if mask & ~((1 << i)-1): raise AssertionError('original order is outside safe domain')
    return result


class Window:
    def __init__(self, bundle, core, left, right, capacity):
        self.core, self.left, self.right = core, left, right
        c = bundle['cores'][core]; order = c['subgraph_order']
        self.original = tuple(order[left:right]); self.m = len(self.original)
        if not 1 <= self.m <= 8: raise ValueError('window must have 1..8 subgraphs')
        self.pred = precedence(bundle['plan_view'], core, left, right)
        self.capacity, self.scale = capacity, scales(capacity)
        self.graph, self.raw = c['graph'], tuple(c['raw_seq'])
        self.rank = {o: i for i, o in enumerate(self.raw)}
        local, touches = events(self.graph)
        ops = {o['id']: o for o in self.graph['ops']}
        outgoing = {o: [] for o in ops}; incoming = {o: [] for o in ops}
        tensor_consumers = {}
        tensors = {t['id']: t for t in self.graph['tensors']}
        for e in self.graph['edges']:
            a, b = e['source'], e['target']
            if a in ops and b in tensors: outgoing[a].append(b)
            if a in tensors and b in ops:
                incoming[b].append(a); tensor_consumers.setdefault(a, []).append(b)
        fixed = {j: [] for j in order}; mobile = []
        win_index = {j: i for i, j in enumerate(self.original)}
        order_rank = {j: i for i, j in enumerate(order)}
        for o in self.raw:
            j = c['op_subgraph'][o]
            if ops[o]['op'] == 'COPY_IN':
                if (len(outgoing[o]) != 1 or len(incoming[o]) != 1 or
                    tensors[incoming[o][0]]['pos'] != 'DDR' or outgoing[o][0] not in local):
                    raise Q2Error('WINDOW_UNSUPPORTED', 'COPY_IN shape')
                consumers = {c['op_subgraph'][v] for v in tensor_consumers.get(outgoing[o][0], [])
                             if ops[v]['op'] not in ('COPY_IN', 'COPY_OUT')}
                if not consumers: raise Q2Error('WINDOW_UNSUPPORTED', 'COPY_IN without compute consumer')
                first = min(consumers, key=order_rank.__getitem__)
                if first != j: raise AssertionError('official COPY_IN bucket differs')
                if not any(order_rank[x] < left for x in consumers):
                    mask = sum(1 << win_index[x] for x in consumers if x in win_index)
                    if mask: mobile.append((o, mask)); continue
            fixed[j].append(o)
        self.fixed = tuple(tuple(fixed[j]) for j in self.original)
        self.mobile = tuple(mobile)
        self.pre_ops = tuple(o for j in order[:left] for o in fixed[j])
        self.post_ops = tuple(o for j in order[right:] for o in fixed[j])
        related_ops = set(o for bucket in self.fixed for o in bucket) | {o for o, _ in mobile}
        related = sorted({t for o in related_ops for t in touches[o]})
        dense = {t: i for i, t in enumerate(related)}
        total = Counter(t for o in self.raw for t in touches[o])
        pre = Counter(t for o in self.pre_ops for t in touches[o])
        self.constant = [0, 0]
        for t, n in pre.items():
            if t not in dense and 0 < n < total[t]:
                p, size = local[t]; self.constant[p] += size
        self.meta = tuple(local[t] for t in related)
        self.total = tuple(total[t] for t in related)
        self.pre = tuple(pre[t] for t in related)
        self.touches = {o: tuple(dense[t] for t in touches[o]) for o in related_ops}
        self.fixed_counts = tuple(Counter(t for o in bucket for t in self.touches[o]) for bucket in self.fixed)
        self._counts = {}
        self.before, self.original_peaks, _ = self.scan_order(tuple(range(self.m)))

    def counts(self, mask):
        if mask not in self._counts:
            result = list(self.pre)
            for j, changes in enumerate(self.fixed_counts):
                if mask & (1 << j):
                    for t, n in changes.items(): result[t] += n
            for o, consumers in self.mobile:
                if consumers & mask:
                    for t in self.touches[o]: result[t] += 1
            self._counts[mask] = tuple(result)
        return self._counts[mask]

    def live(self, counts):
        live = self.constant.copy()
        for i, n in enumerate(counts):
            if 0 < n < self.total[i]:
                p, size = self.meta[i]; live[p] += size
        return live

    def bucket(self, mask, j):
        moving = [o for o, consumers in self.mobile if not consumers & mask and consumers & (1 << j)]
        # fixed 和 moving 均来自 raw_seq，按其 rank 做稳定线性归并。
        return tuple(heapq.merge(self.fixed[j], moving, key=self.rank.__getitem__))

    def transition(self, mask, j, check=lambda: None):
        counts = list(self.counts(mask)); live = self.live(counts); peak = live.copy()
        bucket = self.bucket(mask, j)
        for n, o in enumerate(bucket):
            if n % 128 == 0: check()
            for t in self.touches[o]:
                if counts[t] == 0:
                    p, size = self.meta[t]; live[p] += size
            peak = [max(a, b) for a, b in zip(peak, live)]
            for t in self.touches[o]:
                counts[t] += 1
                if counts[t] == self.total[t]:
                    p, size = self.meta[t]; live[p] -= size
        if tuple(counts) != self.counts(mask | (1 << j)):
            raise AssertionError('DP state event counts depend on history')
        if live != self.live(counts): raise AssertionError('DP exit live differs')
        return max(peak[p]*self.scale[p] for p in (0, 1)), tuple(peak), len(bucket)

    def scan_order(self, order):
        mask = 0; peaks = self.live(self.pre); sequence = []
        for j in order:
            if self.pred[j] & ~mask: raise ValueError('window order violates external precedence')
            _, p, _ = self.transition(mask, j)
            peaks = [max(a, b) for a, b in zip(peaks, p)]
            sequence.extend(self.bucket(mask, j)); mask |= 1 << j
        return max(peaks[p]*self.scale[p] for p in (0, 1)), tuple(peaks), tuple(sequence)


def solve_window(context, check=lambda: None):
    w = context; size = 1 << w.m
    values = [None]*size; paths = [None]*size
    values[0] = max(a*b for a, b in zip(w.live(w.pre), w.scale)); paths[0] = ()
    states = transitions = scanned = 0
    try:
        for mask in range(size):
            check()
            if values[mask] is None: continue
            states += 1
            for j in range(w.m):
                if mask & (1 << j) or w.pred[j] & ~mask: continue
                h, _, count = w.transition(mask, j, check)
                transitions += 1; scanned += count
                nxt = mask | (1 << j); value = max(values[mask], h); path = paths[mask]+(j,)
                if values[nxt] is None or (value, path) < (values[nxt], paths[nxt]):
                    values[nxt], paths[nxt] = value, path
    except BudgetExpired:
        return dict(status='WINDOW_INCOMPLETE', completed=False, original_order=list(w.original),
                    states=states, transitions=transitions, events_scanned=scanned)
    order = paths[-1] if values[-1] < w.before else tuple(range(w.m))
    peak, components, seq = w.scan_order(order)
    if peak != values[-1]: raise AssertionError('DP optimum/backtracking mismatch')
    return dict(status='COMPLETED', completed=True, original_order=list(w.original),
                proposed_order=[w.original[j] for j in order], peak_before=w.before, peak_after=peak,
                components_before=w.original_peaks, components_after=components,
                states=states, transitions=transitions, events_scanned=scanned,
                predicted_seq=list(w.pre_ops)+list(seq)+list(w.post_ops))


def choose_window(bundle, profiles, width, seen):
    candidates = []
    for k, c in bundle['cores'].items():
        order = c['subgraph_order']; m = min(width, len(order))
        if m < 2: continue
        p = profiles[k]; pos = {j: i for i, j in enumerate(order)}
        # 首次超限/首个全核峰值优先，然后逐桶的首次超限/峰值。
        primary = p.first_overflow if p.first_overflow is not None else p.peak_index
        bucket_points = {}
        for i, o in enumerate(c['seq']):
            j = c['op_subgraph'][o]
            scaled = max(p.curve[0][i], 4*p.curve[1][i])
            over = p.curve[0][i] > 524288 or p.curve[1][i] > 131072
            key = (not over, -scaled, i)
            if j not in bucket_points or key < bucket_points[j][0]: bucket_points[j] = (key, i)
        points = [primary]+[i for _, i in sorted(bucket_points.values())]
        for rank, i in enumerate(points):
            if i is None: continue
            pivot = pos[c['op_subgraph'][c['seq'][i]]]
            left = min(max(0, pivot-m//2), len(order)-m); right = left+m
            if (k, left, right) in seen: continue
            over = p.curve[0][i] > 524288 or p.curve[1][i] > 131072
            candidates.append(((rank != 0, not over, -p.scaled, k, left), (k, left, right)))
    return min(candidates)[1] if candidates else None


def apply_order(plan, bundle, w, result, settings, *, profiler=profile):
    updated = {'node_to_subgraph': dict(plan['node_to_subgraph']),
               'core_schedules': [list(q) for q in plan['core_schedules']]}
    updated['core_schedules'][w.core][w.left:w.right] = result['proposed_order']
    fresh = settings.prefix(bundle['_raw'], updated)
    old = bundle['cores'][w.core]; new = fresh['cores'][w.core]
    if new['graph'] != old['graph'] or new['raw_seq'] != old['raw_seq']:
        raise AssertionError('fixed membership reordered the official boundary graph')
    if new['seq'] != result['predicted_seq']: raise AssertionError('DP/official COPY bucket sequence differs')
    a = profiler(old['graph'], old['seq'], settings.capacity)
    b = profiler(new['graph'], new['seq'], settings.capacity)
    if b.scaled > a.scaled: raise AssertionError('full core scalar peak increased')
    fresh['_raw'] = bundle['_raw']
    return updated, fresh
