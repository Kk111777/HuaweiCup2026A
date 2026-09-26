"""Bounded per-case LRU of independent preparation snapshots (no disk pickle I/O)."""
from collections import Counter, OrderedDict
import pickle
from .memory import profile
from .official import digest
from .state import environment_digest, plan_digest, _prepare


class PreparationCache:
    """Private serialized snapshots; get() never returns the cached mutable object.

    The byte bound counts serialized payload plus key bytes, not process RSS.
    Pickle is used only on objects produced in-process, never on untrusted files.
    """
    def __init__(self, max_bytes=64*1024*1024, limits=None):
        self.max_bytes = max_bytes
        self.limits = dict(prefix=8, profile=64, legality=64, exact=256) if limits is None else dict(limits)
        if type(max_bytes) is not int or max_bytes < 0 or any(type(n) is not int or n < 0 for n in self.limits.values()):
            raise ValueError('nonnegative cache limits required')
        self._data = OrderedDict(); self.bytes = 0; self.stats = Counter()

    def get(self, kind, key):
        cache_key = (kind, key)
        if cache_key not in self._data:
            self.stats[kind+'_misses'] += 1
            return None
        payload, _ = self._data[cache_key]; self._data.move_to_end(cache_key)
        self.stats[kind+'_hits'] += 1
        return pickle.loads(payload)

    def _remove(self, key, reason):
        _, size = self._data.pop(key); self.bytes -= size
        self.stats[key[0]+'_'+reason] += 1

    def put(self, kind, key, value):
        cache_key = (kind, key)
        if cache_key in self._data: self._remove(cache_key, 'replacements')
        payload = pickle.dumps(value, protocol=5)
        size = len(payload)+len(repr(cache_key).encode('utf-8'))
        if size > self.max_bytes or self.limits.get(kind, 0) == 0:
            self.stats[kind+'_oversize'] += 1
            return
        while sum(k[0] == kind for k in self._data) >= self.limits[kind]:
            self._remove(next(k for k in self._data if k[0] == kind), 'evictions')
        while self.bytes+size > self.max_bytes:
            self._remove(next(iter(self._data)), 'evictions')
        self._data[cache_key] = payload, size; self.bytes += size

    def invalidate(self):
        for key in list(self._data): self._remove(key, 'invalidations')

    def record(self): return dict(self.stats, payload_bytes=self.bytes, entries=len(self._data), max_bytes=self.max_bytes)


class PreparationService:
    def __init__(self, graph, cache=None):
        self.g = graph; self.namespace = environment_digest(graph)
        self.cache = cache if cache is not None else PreparationCache()
        self._configuration = self._settings_key()

    def _settings_key(self):
        return digest(dict(capacity=self.g.settings.capacity, bandwidth=self.g.settings.bandwidth,
                           delay=self.g.settings.delay, official=self.g.settings.hashes))

    def _refresh_configuration(self):
        current = self._settings_key()
        if current != self._configuration:
            self.namespace = environment_digest(self.g); self._configuration = current
            self.cache.stats['namespace_invalidations'] += 1

    def prefix(self, plan):
        self._refresh_configuration()
        key = (self.namespace, plan_digest(plan))
        value = self.cache.get('prefix', key)
        if value is None:
            value = self.g.settings.prefix(self.g.raw, plan)
            self.cache.stats['prefix_builds'] += 1
            self.cache.put('prefix', key, value)
        return value

    def profiles(self, bundle):
        self._refresh_configuration()
        result = {}
        for k, c in bundle['cores'].items():
            key = (self.namespace, digest(dict(graph=c['graph'], seq=c['seq'])))
            value = self.cache.get('profile', key)
            if value is None:
                value = profile(c['graph'], c['seq'], self.g.settings.capacity)
                self.cache.stats['profile_builds'] += 1
                self.cache.put('profile', key, value)
            result[k] = value
        return result

    def prepare(self, plan, **kwargs):
        return _prepare(self.g, plan, self.prefix(plan), self.namespace, **kwargs)
