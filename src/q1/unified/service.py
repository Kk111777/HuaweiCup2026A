"""Q1 compatibility constructor for the shared evaluation service."""
from scheduling.service import SearchServices as _SearchServices


class SearchServices(_SearchServices):
    def __init__(self, model, settings, *, checkpoint=None, progress=None, save=None,
                 verified=None, rejected=None):
        super().__init__(model, settings, problem='q1', checkpoint=checkpoint,
                         progress=progress, save=save, verified=verified, rejected=rejected)
