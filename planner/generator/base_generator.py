"""Base Generator Module.

重构说明：
v4 已删除 ``custom_model`` 段（决策：强制走主程序 ``model_config.toml`` 任务名）
并删除 ``src.config.api_ada_configs`` 临时注册 hack。``get_model_config`` 现在
仅返回主程序中 ``model_config`` 预先配置的任务名字符串（默认 ``replyer``）。

职责（重构后）：
    - 模型任务名解析（供 ScheduleGenerator 在调 ``ctx.llm.generate`` 时使用）
    - 组件协调（PromptBuilder、SchemaBuilder、ContextLoader）
    - 向后兼容的 API

已移除职责（迁移到专门组件）：
    - Prompt 构建 → PromptBuilder
    - Schema 构建 → SchemaBuilder
    - 上下文加载 → ScheduleContextLoader
    - 时区管理 → TimezoneManager
"""

from typing import Any, Dict, List, Optional, Tuple
import logging

from ...utils.timezone_manager import TimezoneManager
from ..goal_manager import GoalManager
from .context_loader import ScheduleContextLoader
from .prompt_builder import PromptBuilder
from .schema_builder import SchemaBuilder

logger = logging.getLogger(__name__)


# 默认模型任务名（需在主程序 model_config.toml 中预先配置）
DEFAULT_LLM_TASK_NAME = "replyer"
DEFAULT_TEMPERATURE = 0.7


class BaseScheduleGenerator:
    """基础日程生成器 - 提供配置和工具方法（重构版）

    职责（重构后）：
        - 模型任务名管理
        - 组件协调（PromptBuilder、SchemaBuilder、ContextLoader）
        - 向后兼容的 API
    """

    def __init__(self, goal_manager: GoalManager, config: Optional[Dict[str, Any]] = None) -> None:
        """初始化基础生成器。

        Args:
            goal_manager: 目标管理器
            config: 配置字典（可选）
        """
        self.goal_manager = goal_manager
        self.yesterday_schedule_summary: Optional[str] = None  # 昨日日程摘要（用于上下文）
        self.config: Dict[str, Any] = config or {}  # 保存配置

        # 初始化时区管理器
        timezone_str = self.config.get("timezone", "Asia/Shanghai")
        self.tz_manager = TimezoneManager(timezone_str)

        # 初始化组件（依赖注入）
        self.prompt_builder = PromptBuilder(self.config, self.tz_manager)
        self.schema_builder = SchemaBuilder(self.config)
        self.context_loader = ScheduleContextLoader(goal_manager, self.tz_manager)

    # ========================================================================
    # 模型配置
    # ========================================================================

    def get_model_config(self) -> Tuple[str, int, float]:
        """获取 LLM 调用参数。

        v4 简化版本：返回主程序 ``model_config.toml`` 中预先配置的任务名字符串，
        由 ScheduleGenerator 在调用 ``ctx.llm.generate(model=task_name, ...)`` 时使用。

        Returns:
            ``(task_name, max_tokens, temperature)`` 三元组
        """
        # v4 不再支持插件内 custom_model，统一走主程序 model_config 任务名
        task_name = str(self.config.get("llm_task_name", DEFAULT_LLM_TASK_NAME)).strip() or DEFAULT_LLM_TASK_NAME
        max_tokens = int(self.config.get("max_tokens", 8192))
        temperature = float(self.config.get("temperature", DEFAULT_TEMPERATURE))

        logger.debug(f"LLM 任务名: {task_name} (max_tokens={max_tokens}, temperature={temperature})")
        return task_name, max_tokens, temperature

    # ========================================================================
    # 向后兼容的委托方法（调用新组件）
    # ========================================================================

    def build_json_schema(self) -> dict:
        """构建 JSON Schema（委托给 SchemaBuilder）。

        Returns:
            JSON Schema 字典
        """
        return self.schema_builder.build_json_schema()

    def load_yesterday_schedule_summary(self) -> Optional[str]:
        """加载昨日日程摘要（委托给 ContextLoader）。

        Returns:
            昨日日程摘要字符串
        """
        summary = self.context_loader.load_yesterday_schedule_summary()
        self.yesterday_schedule_summary = summary  # 保存到实例变量（向后兼容）
        return summary

    def build_schedule_prompt(
        self,
        schedule_type: Any,
        preferences: Dict[str, Any],
        schema: Optional[Dict] = None,
    ) -> str:
        """构建日程生成提示词（委托给 PromptBuilder）。

        Args:
            schedule_type: 日程类型
            preferences: 用户偏好
            schema: JSON Schema（可选）

        Returns:
            提示词字符串
        """
        return self.prompt_builder.build_schedule_prompt(
            schedule_type,
            preferences,
            schema,
            self.yesterday_schedule_summary,
        )

    def build_retry_prompt(
        self,
        schedule_type: Any,
        preferences: Dict[str, Any],
        schema: Dict,
        previous_issues: List[str],
    ) -> str:
        """构建第二轮 prompt（委托给 PromptBuilder）。

        Args:
            schedule_type: 日程类型
            preferences: 用户偏好
            schema: JSON Schema
            previous_issues: 上一轮的问题列表

        Returns:
            改进后的提示词
        """
        return self.prompt_builder.build_retry_prompt(
            schedule_type,
            preferences,
            schema,
            previous_issues,
            self.yesterday_schedule_summary,
        )
