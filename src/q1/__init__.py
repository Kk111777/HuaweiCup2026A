"""问题一：就绪生长与多核列表分配初始化。"""

from .solver import InitializationResult, initialize
from .official import Settings

__all__ = ["InitializationResult", "Settings", "initialize"]
