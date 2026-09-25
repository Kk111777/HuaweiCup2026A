"""Shared deterministic list allocation with explicit heuristic tie policies."""
import heapq
from .model import topological


def allocate(pred, succ, costs, cores, same, cross, *, priority="critical",
             fifo=None, preferred=None, tie="affinity"):
    order = topological(pred, succ)
    ranks = {}
    for i in reversed(order):
        ranks[i] = costs[i] + max((ranks[j] for j in succ[i]), default=0)
    def rank(i):
        return fifo[i] if priority == "fifo" else -costs[i] if priority == "largest" else -ranks[i]
    ready = [(rank(i), i) for i in order if not pred[i]]
    heapq.heapify(ready)
    degrees = {i: len(pred[i]) for i in order}
    queues, ends, assigned, records = [[] for _ in range(cores)], {}, {}, []
    while ready:
        _, i = heapq.heappop(ready)
        options = []
        for c, queue in enumerate(queues):
            available = ends[queue[-1]] + same if queue else 0
            start = max([available] + [ends[p] + (cross if assigned[p] != c else 0) for p in pred[i]])
            end = start + costs[i]
            key = ((end, available, c) if tie == "available" else
                   (end, int(preferred is not None and preferred.get(i) != c), len(queue), c))
            options.append((key, c, start, end, available))
        _, c, start, end, available = min(options)
        queues[c].append(i)
        ends[i], assigned[i] = end, c
        records.append({"task": i, "core": c, "reference_start": start,
                        "reference_end": end, "core_available": available, "tail_cycles": ranks[i]})
        for j in sorted(succ[i]):
            degrees[j] -= 1
            if not degrees[j]:
                heapq.heappush(ready, (rank(j), j))
    if len(ends) != len(costs):
        raise ValueError("allocation did not cover every task")
    return queues, ends, records
