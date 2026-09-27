# Adapted from Q2-Optimized-3.0 commit 29747d80dac80a45a7902af35e2aa58b526ebbf6.
# Local Q3-owned copy; no runtime dependency on a Q2 checkout.
"""Exact, capacity-feasible minimum-disruption ordering for fixed core windows.

Reuses the official *input* semantics through a read-only Step1 prefix. It never
changes an official function and never equates minimum disruption with makespan.
A search block may contain several adjacent submitted sgids; they remain ordered
inside the block, avoiding the '8 singleton subgraphs = only 8 operations' trap.
"""
from collections import Counter
import heapq
from .memory_lifetimes import events


def profile(graph, seq, capacity):
    """Reference-format adapter; the production interval scan lives in memory.py."""
    from .memory_lifetimes import profile as production_profile
    value = production_profile(graph, seq, capacity)
    return dict(curves=[list(row) for row in value.curve], peaks=list(value.peaks),
                first_overflow=value.first_overflow, no_spill_certificate=value.no_spill)


def window_predecessors(view, core, blocks):
    order = view['core_orders'][core]
    flat = [j for block in blocks for j in block]
    left = order.index(flat[0]); right = left+len(flat)
    if order[left:right] != flat: raise ValueError('search blocks must be one contiguous core window')
    owner = {j: i for i, block in enumerate(blocks) for j in block}
    succ = {j: set() for j in view['subgraph_ids']}
    for a, b in view['dependency_pairs']: succ[a].add(b)
    for k, queue in view['core_orders'].items():
        for a, b in zip(queue, queue[1:]):
            if k == core and a in owner and b in owner and owner[a] != owner[b]: continue
            succ[a].add(b)
    if left:
        for j in flat: succ[order[left-1]].add(j)
    if right < len(order):
        for j in flat: succ[j].add(order[right])
    degree = dict.fromkeys(succ, 0); reach = dict.fromkeys(succ, 0)
    for ns in succ.values():
        for b in ns: degree[b] += 1
    ready = [j for j,n in degree.items() if not n]; heapq.heapify(ready); visited = 0
    while ready:
        a = heapq.heappop(ready); visited += 1
        bit = (1 << owner[a]) if a in owner else 0
        for b in sorted(succ[a]):
            reach[b] |= reach[a] | bit; degree[b] -= 1
            if not degree[b]: heapq.heappush(ready, b)
    if visited != len(succ): raise ValueError('window anchors introduce a cycle')
    pred = []
    for i, block in enumerate(blocks):
        mask = 0
        for j in block: mask |= reach[j]
        mask &= ~(1 << i)  # paths inside a block may legally traverse outside nodes
        if mask & ~((1 << i)-1): raise ValueError('original block order outside safe domain')
        pred.append(mask)
    return left, right, tuple(pred)


class CapacityWindow:
    def __init__(self, bundle, core, blocks, capacity):
        self.core = core; self.blocks = tuple(tuple(b) for b in blocks)
        self.m = len(blocks)
        if not 1 <= self.m <= 8: raise ValueError('1..8 search blocks required')
        self.left, self.right, self.pred = window_predecessors(bundle['plan_view'], core, self.blocks)
        c = bundle['cores'][core]; self.graph = c['graph']; self.raw = tuple(c['raw_seq'])
        self.order = c['subgraph_order']; self.caps = (capacity['L1'], capacity['UB'])
        local, touches = events(self.graph)
        op_by_id = {o['id']:o for o in self.graph['ops']}
        rank = {o:i for i,o in enumerate(self.raw)}; self.rank = rank
        subgroup_rank = {j:i for i,j in enumerate(self.order)}
        block_owner = {j:i for i,b in enumerate(self.blocks) for j in b}
        self.inner_rank = [{sg:p for p,sg in enumerate(b)} for b in self.blocks]
        out = {o:[] for o in op_by_id}; users = {}
        for e in self.graph['edges']:
            a,b = e['source'],e['target']
            if a in op_by_id and b not in op_by_id: out[a].append(b)
            if b in op_by_id and a not in op_by_id: users.setdefault(a,[]).append(b)
        fixed = {j:[] for j in self.order}; mobile = []
        for o in self.raw:
            sg = c['op_subgraph'][o]
            if op_by_id[o]['op'] == 'COPY_IN':
                if len(out[o]) != 1: raise ValueError('nonstandard COPY_IN')
                consumers = {c['op_subgraph'][v] for v in users.get(out[o][0], [])
                             if op_by_id[v]['op'] not in ('COPY_IN','COPY_OUT')}
                if not consumers: raise ValueError('COPY_IN has no computation consumer')
                if not any(subgroup_rank[j] < self.left for j in consumers):
                    inside = consumers & block_owner.keys()
                    if inside:
                        mask = 0; target_sg = {}
                        for j in inside:
                            b = block_owner[j]; mask |= 1 << b
                            if b not in target_sg or self.inner_rank[b][j] < self.inner_rank[b][target_sg[b]]:
                                target_sg[b] = j
                        mobile.append((o, mask, target_sg)); continue
            fixed[sg].append(o)
        self.fixed = tuple(tuple(o for j in block for o in fixed[j]) for block in self.blocks)
        self.pre_ops = tuple(o for j in self.order[:self.left] for o in fixed[j])
        self.post_ops = tuple(o for j in self.order[self.right:] for o in fixed[j])
        self.mobile = tuple(mobile); self.op_sg = c['op_subgraph']
        related_ops = set(o for bucket in self.fixed for o in bucket) | {o for o,_,_ in mobile}
        related = sorted({t for o in related_ops for t in touches[o]})
        dense = {t:i for i,t in enumerate(related)}
        total = Counter(t for o in self.raw for t in touches[o]); pre = Counter(t for o in self.pre_ops for t in touches[o])
        self.constant = [0,0]
        for t,n in pre.items():
            if t not in dense and 0 < n < total[t]:
                p,size = local[t]; self.constant[p] += size
        self.meta = tuple(local[t] for t in related)
        self.total = tuple(total[t] for t in related); self.pre = tuple(pre[t] for t in related)
        self.touches = {o:tuple(dense[t] for t in touches[o]) for o in related_ops}
        self.fixed_counts = [Counter(t for o in bucket for t in self.touches[o]) for bucket in self.fixed]
        self._counts = {}; self._transition = {}

    def counts(self, mask):
        if mask not in self._counts:
            a = list(self.pre)
            for i, cs in enumerate(self.fixed_counts):
                if mask & (1<<i):
                    for t,n in cs.items(): a[t] += n
            for o, consumers, _ in self.mobile:
                if consumers & mask:
                    for t in self.touches[o]: a[t] += 1
            self._counts[mask] = tuple(a)
        return self._counts[mask]

    def live(self, counts):
        a = self.constant.copy()
        for t,n in enumerate(counts):
            if 0 < n < self.total[t]:
                p,size = self.meta[t]; a[p] += size
        return a

    def bucket(self, mask, j):
        items = [(self.inner_rank[j][self.op_sg[o]],self.rank[o],o) for o in self.fixed[j]]
        for o, consumers, target_sg in self.mobile:
            if not consumers & mask and consumers & (1<<j):
                items.append((self.inner_rank[j][target_sg[j]],self.rank[o],o))
        return tuple(o for _,_,o in sorted(items))

    def transition(self, mask, j, check=lambda: None):
        key = (mask,j)
        if key not in self._transition:
            counts = list(self.counts(mask)); live = self.live(counts); peak = live.copy()
            bucket = self.bucket(mask,j)
            for i,o in enumerate(bucket):
                if i%128 == 0: check()
                for t in self.touches[o]:
                    if counts[t] == 0:
                        p,size = self.meta[t]; live[p] += size
                peak = [max(a,b) for a,b in zip(peak,live)]
                for t in self.touches[o]:
                    counts[t] += 1
                    if counts[t] == self.total[t]:
                        p,size = self.meta[t]; live[p] -= size
            if tuple(counts) != self.counts(mask | (1<<j)):
                raise AssertionError('subset state is not history independent')
            self._transition[key] = (tuple(peak),len(bucket))
        return self._transition[key]

    def scan(self, order):
        mask = 0; peak = self.live(self.pre); seq = list(self.pre_ops)
        for j in order:
            if mask & (1<<j) or self.pred[j] & ~mask: raise ValueError('illegal block order')
            p,_ = self.transition(mask,j); peak = [max(a,b) for a,b in zip(peak,p)]
            seq.extend(self.bucket(mask,j)); mask |= 1<<j
        if mask != (1<<self.m)-1: raise ValueError('incomplete block order')
        return tuple(peak), seq+list(self.post_ops)


def minimum_disruption(window, check=lambda: None):
    """Exact inversion minimum under BOTH cache capacities, fixed block domain."""
    w = window; size = 1<<w.m
    best = [None]*size; path = [None]*size; best[0]=0; path[0]=()
    states=transitions=scanned=0
    if any(a>b for a,b in zip(w.live(w.pre),w.caps)):
        return {'status':'NO_FEASIBLE_ORDER_IN_FIXED_WINDOW','reason':'entry_live_exceeds_capacity',
                'states': 0, 'transitions': 0, 'events_scanned': 0}
    for mask in range(size):
        check()
        if best[mask] is None: continue
        states += 1
        for j in range(w.m):
            if mask & (1<<j) or w.pred[j]&~mask: continue
            p,count=w.transition(mask,j,check); transitions+=1; scanned+=count
            if any(a>b for a,b in zip(p,w.caps)): continue
            nxt=mask|(1<<j)
            value=best[mask]+(((1<<j)-1)&~mask).bit_count()
            candidate=path[mask]+(j,)
            if best[nxt] is None or (value,candidate)<(best[nxt],path[nxt]):
                best[nxt],path[nxt]=value,candidate
    stats={'states':states,'transitions':transitions,'events_scanned':scanned}
    if path[-1] is None:
        return {'status':'NO_FEASIBLE_ORDER_IN_FIXED_WINDOW',**stats}
    peaks, seq=w.scan(path[-1])
    return {'status':'OPTIMAL_IN_FIXED_WINDOW','inversions':best[-1],
            'block_order':path[-1], 'subgraph_order':[sg for j in path[-1] for sg in w.blocks[j]],
            'peaks':peaks,'predicted_seq':seq,**stats}


def apply_window(g, plan, old_bundle, w, answer, prefix_fn):
    if answer['status']!='OPTIMAL_IN_FIXED_WINDOW': raise ValueError('no solution to apply')
    new={'node_to_subgraph':dict(plan['node_to_subgraph']),
         'core_schedules':[list(q) for q in plan['core_schedules']]}
    new['core_schedules'][w.core][w.left:w.right]=answer['subgraph_order']
    bundle=prefix_fn(g,new)
    if bundle['cores'][w.core]['seq']!=answer['predicted_seq']:
        raise AssertionError('dynamic COPY regrouping differs from official Step1')
    for k in bundle['cores']:
        if bundle['cores'][k]['graph']!=old_bundle['cores'][k]['graph']:
            raise AssertionError('window reordering changed fixed core graph')
        if k!=w.core and bundle['cores'][k]['seq']!=old_bundle['cores'][k]['seq']:
            raise AssertionError('unmodified core sequence changed')
    if bundle['cross_links']!=old_bundle['cross_links']: raise AssertionError('cross links changed')
    return new,bundle
