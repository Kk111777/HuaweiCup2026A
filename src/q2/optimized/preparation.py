"""V3 preparation services, extending the unchanged v2.1 component interfaces."""
from q2.preparation import PreparationCache as BaselineCache
from q2.preparation import PreparationService as BaselinePreparation


class PreparationCache(BaselineCache):
    def __init__(self, max_bytes=64*1024*1024, limits=None):
        if limits is None:
            limits = dict(prefix=8, profile=64, legality=64, exact=256,
                          prepared_core=16, execution=4, model=64)
        super().__init__(max_bytes, limits)


class PreparationService(BaselinePreparation):
    def __init__(self, graph, cache=None):
        super().__init__(graph, cache if cache is not None else PreparationCache())

    def prepare_execution(self, state, *, check=lambda: None):
        """Reuse official Step2/3 preparation without running the global simulator."""
        from .prepared import prepare_execution
        return prepare_execution(self, state, check=check)
