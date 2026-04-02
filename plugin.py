"""麦麦自主规划插件 - 主文件"""

import asyncio
from typing import List, Tuple

from src.plugin_system import BasePlugin, register_plugin, ConfigField
from src.common.logger import get_logger

from .tools import ManageGoalTool, GetPlanningStatusTool, GenerateScheduleTool, ApplyScheduleTool
from .handlers import AutonomousPlannerEventHandler, ScheduleInjectEventHandler
from .commands import PlanningCommand
from .planner.auto_scheduler import ScheduleAutoScheduler

logger = get_logger("autonomous_planning")

@register_plugin
class AutonomousPlanningPlugin(BasePlugin):
    """麦麦自主规划插件"""

    plugin_name: str = "autonomous_planning_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []  # perception_plugin 是可选依赖
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本配置",
        "autonomous_planning": "自主规划总配置",
        "autonomous_planning.schedule": "日程管理配置",
        "autonomous_planning.schedule.inject": "智能注入配置",
        "autonomous_planning.schedule.custom_model": "自定义模型配置"
    }

    config_schema: dict = {
        "plugin": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用插件"
            ),
        },
        "autonomous_planning": {
            "cleanup_interval": ConfigField(
                type=int,
                default=3600,
                description="清理间隔（秒）"
            ),
            "cleanup_old_goals_days": ConfigField(
                type=int,
                default=30,
                description="保留历史记录天数"
            ),
            "schedule": {
                # 日程注入功能
                "inject_schedule": ConfigField(
                    type=bool,
                    default=True,
                    description="在对话时自然提到当前活动"
                ),
                "auto_generate": ConfigField(
                    type=bool,
                    default=True,
                    description="询问日程时自动检查并生成"
                ),
                # 🆕 智能注入配置（v1.1.0新增）
                "inject": {
                    "inject_mode": ConfigField(
                        type=str,
                        default="smart",
                        description="注入模式：smart(智能注入) 或 traditional(传统模式)"
                    ),
                    "enable_intent_classification": ConfigField(
                        type=bool,
                        default=True,
                        description="启用意图分类（识别用户询问类型）"
                    ),
                    "enable_state_analysis": ConfigField(
                        type=bool,
                        default=True,
                        description="启用状态分析（生成情感化活动描述）"
                    ),
                    "enable_inject_optimization": ConfigField(
                        type=bool,
                        default=True,
                        description="启用注入优化（防止重复注入和无效打扰）"
                    ),
                    "casual_chat_inject_probability": ConfigField(
                        type=float,
                        default=0.5,
                        description="闲聊时的注入概率（0.0-1.0）"
                    ),
                    "context_max_turns": ConfigField(
                        type=int,
                        default=3,
                        description="对话上下文保留轮数"
                    ),
                    "context_ttl": ConfigField(
                        type=int,
                        default=600,
                        description="对话上下文过期时间（秒）"
                    ),
                },
                # 🎨 自定义提示词配置
                "custom_prompt": ConfigField(
                    type=str,
                    default="",
                    description="自定义日程生成提示词（如\"今天想多运动\"、\"专注学习\"等，留空则使用默认风格）"
                ),
                "auto_infer_next_day_prompt": ConfigField(
                    type=bool,
                    default=False,
                    description="是否在晚间自动推断次日策略提示词（默认关闭）"
                ),
                "infer_time": ConfigField(
                    type=str,
                    default="22:30",
                    description="次日策略推断时间（HH:MM格式）"
                ),
                "infer_lookback_days": ConfigField(
                    type=int,
                    default=3,
                    description="次日策略推断时回看历史天数（1-7）"
                ),
                "infer_max_prompt_chars": ConfigField(
                    type=int,
                    default=300,
                    description="次日策略推断结果最大字符数"
                ),
                "infer_use_completion_signal": ConfigField(
                    type=bool,
                    default=True,
                    description="推断时是否参考活动状态和进度"
                ),
                "max_future_activities": ConfigField(
                    type=int,
                    default=3,
                    description="智能注入时最多显示的未来活动数量"
                ),
                # 🎯 多轮生成配置
                "use_multi_round": ConfigField(
                    type=bool,
                    default=True,
                    description="启用多轮生成机制（通过多轮优化提升日程质量）"
                ),
                "max_rounds": ConfigField(
                    type=int,
                    default=2,
                    description="最多生成轮数（1-3轮，推荐2轮）"
                ),
                "quality_threshold": ConfigField(
                    type=float,
                    default=0.85,
                    description="质量阈值（0.80-0.90，达到此分数即停止优化）"
                ),
                # 📊 生成参数配置
                "min_activities": ConfigField(
                    type=int,
                    default=8,
                    description="最少活动数量（建议8-10个）"
                ),
                "max_activities": ConfigField(
                    type=int,
                    default=15,
                    description="最多活动数量（建议12-15个）"
                ),
                "enable_detailed_description": ConfigField(
                    type=bool,
                    default=True,
                    description="是否启用详细活动描述（关闭后生成、注入、命令都不显示详细描述）"
                ),
                "min_description_length": ConfigField(
                    type=int,
                    default=20,
                    description="活动描述最小字符数"
                ),
                "max_description_length": ConfigField(
                    type=int,
                    default=50,
                    description="活动描述最大字符数"
                ),
                "max_tokens": ConfigField(
                    type=int,
                    default=8192,
                    description="AI生成的最大token数"
                ),
                "generation_timeout": ConfigField(
                    type=float,
                    default=180.0,
                    description="单次生成超时时间（秒，推荐120-300秒）"
                ),
                # 💾 缓存配置
                "cache_ttl": ConfigField(
                    type=int,
                    default=300,
                    description="日程缓存有效期（秒，默认5分钟）"
                ),
                "cache_max_size": ConfigField(
                    type=int,
                    default=100,
                    description="缓存最大条目数（LRU策略）"
                ),
                # ⏰ 定时自动生成配置
                "auto_schedule_enabled": ConfigField(
                    type=bool,
                    default=True,
                    description="每天定时自动生成日程"
                ),
                "auto_schedule_time": ConfigField(
                    type=str,
                    default="00:30",
                    description="自动生成时间（HH:MM格式，如00:30表示凌晨0点30分）"
                ),
                "timezone": ConfigField(
                    type=str,
                    default="Asia/Shanghai",
                    description="时区设置（如Asia/Shanghai、UTC等）"
                ),
                # 🔐 权限配置
                "admin_users": ConfigField(
                    type=list,
                    default=[],
                    description="管理员QQ号列表（如[\\\"12345\\\", \\\"67890\\\"]，留空则所有人可用）"
                ),
                # 🤖 自定义模型配置
                "custom_model": {
                    "enabled": ConfigField(
                        type=bool,
                        default=False,
                        description="使用自定义AI模型（不使用主回复模型）"
                    ),
                    "model_name": ConfigField(
                        type=str,
                        default="",
                        description="模型名称（如gpt-4、claude-3-opus等）"
                    ),
                    "api_base": ConfigField(
                        type=str,
                        default="",
                        description="API地址（如https://api.openai.com/v1）"
                    ),
                    "api_key": ConfigField(
                        type=str,
                        default="",
                        description="API密钥（建议使用环境变量）"
                    ),
                    "provider": ConfigField(
                        type=str,
                        default="",
                        description="提供商类型（openai、anthropic、azure等）"
                    ),
                    "temperature": ConfigField(
                        type=float,
                        default=0.7,
                        description="生成温度参数（0.0-1.0，越高越随机）"
                    ),
                },
            },
        },
    }

    def __init__(self, *args, **kwargs):
        """初始化插件"""
        super().__init__(*args, **kwargs)
        self.scheduler = None
        logger.debug("自主规划插件初始化完成")
        # 延迟启动调度器，确保插件系统完全初始化
        asyncio.create_task(self._start_scheduler_after_delay())

    async def _start_scheduler_after_delay(self):
        """延迟启动调度器（10秒后）"""
        await asyncio.sleep(10)
        self.scheduler = ScheduleAutoScheduler(self)
        await self.scheduler.start()

    def get_plugin_components(self) -> List[Tuple]:
        """获取插件组件"""
        return [
            # Tools - 供 LLM 直接调用的工具
            (ManageGoalTool.get_tool_info(), ManageGoalTool),
            (GetPlanningStatusTool.get_tool_info(), GetPlanningStatusTool),
            (GenerateScheduleTool.get_tool_info(), GenerateScheduleTool),
            (ApplyScheduleTool.get_tool_info(), ApplyScheduleTool),
            # Event Handlers - 事件处理器
            (AutonomousPlannerEventHandler.get_handler_info(), AutonomousPlannerEventHandler),
            (ScheduleInjectEventHandler.get_handler_info(), ScheduleInjectEventHandler),
            # Commands - 命令处理
            (PlanningCommand.get_command_info(), PlanningCommand),
        ]
