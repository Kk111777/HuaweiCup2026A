"""Deprecated compatibility forwarding; default repair DP is q2.capacity."""
import warnings
from .legacy import window as _legacy

def __getattr__(name):
    if name.startswith("_") or not hasattr(_legacy, name): raise AttributeError(name)
    warnings.warn("Use q2.legacy.window explicitly", DeprecationWarning, stacklevel=2)
    return getattr(_legacy, name)
