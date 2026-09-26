"""A single bounded state pool with separately retained search and exact bests."""
from collections import OrderedDict, defaultdict
from functools import cmp_to_key

from q2.evaluation import better, reconcile


def compare(a, b):
    return -1 if better(a.score, b.score) else 1 if better(b.score, a.score) else 0


class StatePool:
    def __init__(self, size=6):
        if type(size) is not int or size < 2: raise ValueError('pool size must be at least two')
        self.size = size
        self.rows = OrderedDict()
        self.traces = {}
        self.incumbents = reconcile(())
        self.evictions = 0

    def update(self, rows, traces=()):
        incoming = list(rows)
        combined = reconcile([*self.rows.values(), *incoming])
        ordered = sorted(combined.candidates, key=cmp_to_key(compare))
        selected = OrderedDict()
        for row in (combined.search, combined.exact):
            if row is not None: selected[row.state.state_signature] = row
        groups = {(row.state.core_of_op, row.state.origin.family, row.state.origin.encoding)
                  for row in selected.values()}
        for row in ordered:
            key = row.state.core_of_op, row.state.origin.family, row.state.origin.encoding
            if len(selected) < self.size and key not in groups:
                selected[row.state.state_signature] = row; groups.add(key)
        for row in ordered:
            if len(selected) >= self.size: break
            selected.setdefault(row.state.state_signature, row)
        self.evictions += len(set(self.rows)-set(selected))
        self.rows = selected
        self.traces.update(traces)
        self.traces = {key: trace for key, trace in self.traces.items() if key in selected}
        self.incumbents = reconcile(selected.values())
        self.rows = OrderedDict((r.state.state_signature, r) for r in self.incumbents.candidates)
        return self.incumbents

    def recalibrate(self, evaluator):
        from dataclasses import replace
        updated = []
        for signature, row in self.rows.items():
            if signature in self.traces:
                self.traces[signature] = evaluator.rescore(row.state, self.traces[signature])
                if not row.score.exact: row = replace(row, score=self.traces[signature].score)
            updated.append(row)
        return self.update(updated)


def select_batch(entries, limit=12, minimum=2, rotation=0):
    """Reserve family quotas, then fill globally; stable ties preserve arrival."""
    families = defaultdict(list)
    for i, entry in enumerate(entries):
        families[entry['proposal'].family].append((entry, i))
    def key(pair):
        entry, i = pair
        trace = entry.get('coarse')
        return trace.score.makespan if trace else float('inf'), i
    names = list(families)
    if names: names = names[rotation % len(names):] + names[:rotation % len(names)]
    for name in names: families[name].sort(key=key)
    selected, seen = [], set()
    for j in range(minimum):
        for name in names:
            if len(selected) >= limit: break
            if len(families[name]) > j:
                entry, i = families[name][j]; selected.append(entry); seen.add(i)
    remainder = sorted((pair for values in families.values() for pair in values if pair[1] not in seen), key=key)
    selected.extend(entry for entry, _ in remainder[:max(0, limit-len(selected))])
    return selected
