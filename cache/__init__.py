"""缓存模块 - 自主规划插件

提供线程/协程安全的 LRU 缓存实现。
"""

from .lru_cache import LRUCache

__all__ = ["LRUCache"]
