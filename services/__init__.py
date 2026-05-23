"""自主规划插件 v4 - Service 层。

把旧版 BaseTool / BaseCommand / BaseEventHandler 的业务实现拆分到 service，
plugin.py 中只保留装饰器外壳，调用对应 service 方法完成具体逻辑。

阶段 1 阶段所有 service 仅给出最小占位实现，方法签名稳定，
阶段 2 起填入实际业务（从 v3 代码迁移）。
"""

from .cleanup_service import CleanupService
from .command_service import CommandService
from .inject_service import InjectService
from .tools_service import ToolsService

__all__ = [
    "CleanupService",
    "CommandService",
    "InjectService",
    "ToolsService",
]
