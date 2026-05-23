"""自主规划插件 v4 - 配置模型

把 v3 的 ``config_schema`` 字典翻译为 ``PluginConfigBase`` 嵌套模型。
保持三级嵌套结构（plugin / autonomous_planning / schedule / schedule.inject）
以兼容用户既有 ``config.toml``。

按决策：v4 删除 ``schedule.custom_model`` 段，强制走主程序 ``model_config.toml`` 任务名。
"""

from typing import List

from maibot_sdk import Field, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置段：[plugin]"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="4.0.0", description="配置版本号（迁移与兼容性识别）")


class InjectConfig(PluginConfigBase):
    """智能注入配置段：[autonomous_planning.schedule.inject]"""

    __ui_label__ = "智能注入"
    __ui_icon__ = "zap"
    __ui_order__ = 0

    inject_mode: str = Field(default="smart", description="注入模式：smart(智能注入) 或 traditional(传统模式)")
    enable_intent_classification: bool = Field(default=True, description="启用意图分类（识别用户询问类型）")
    enable_state_analysis: bool = Field(default=True, description="启用状态分析（生成情感化活动描述）")
    enable_inject_optimization: bool = Field(default=True, description="启用注入优化（防止重复注入和无效打扰）")
    casual_chat_inject_probability: float = Field(default=0.5, description="闲聊时的注入概率（0.0-1.0）")
    context_max_turns: int = Field(default=3, description="对话上下文保留轮数")
    context_ttl: int = Field(default=600, description="对话上下文过期时间（秒）")


class ScheduleConfig(PluginConfigBase):
    """日程管理配置段：[autonomous_planning.schedule]"""

    __ui_label__ = "日程管理"
    __ui_icon__ = "calendar"
    __ui_order__ = 1

    # 日程注入功能开关
    inject_schedule: bool = Field(default=True, description="在对话时自然提到当前活动")
    auto_generate: bool = Field(default=True, description="询问日程时自动检查并生成")

    # 🎨 自定义提示词
    custom_prompt: str = Field(
        default="",
        description="自定义日程生成提示词（如\"今天想多运动\"、\"专注学习\"等，留空则使用默认风格）",
    )

    # 次日策略推断
    auto_infer_next_day_prompt: bool = Field(default=False, description="是否在晚间自动推断次日策略提示词（默认关闭）")
    infer_time: str = Field(default="22:30", description="次日策略推断时间（HH:MM 格式）")
    infer_lookback_days: int = Field(default=3, description="次日策略推断时回看历史天数（1-7）")
    infer_max_prompt_chars: int = Field(default=300, description="次日策略推断结果最大字符数")
    infer_use_completion_signal: bool = Field(default=True, description="推断时是否参考活动状态和进度")

    # 注入显示
    max_future_activities: int = Field(default=3, description="智能注入时最多显示的未来活动数量")

    # 🎯 多轮生成
    use_multi_round: bool = Field(default=True, description="启用多轮生成机制（通过多轮优化提升日程质量）")
    max_rounds: int = Field(default=2, description="最多生成轮数（1-3 轮，推荐 2 轮）")
    quality_threshold: float = Field(default=0.85, description="质量阈值（0.80-0.90，达到此分数即停止优化）")

    # 📊 生成参数
    min_activities: int = Field(default=8, description="最少活动数量（建议 8-10 个）")
    max_activities: int = Field(default=15, description="最多活动数量（建议 12-15 个）")
    enable_detailed_description: bool = Field(
        default=True,
        description="是否启用详细活动描述（关闭后生成、注入、命令都不显示详细描述）",
    )
    min_description_length: int = Field(default=20, description="活动描述最小字符数")
    max_description_length: int = Field(default=50, description="活动描述最大字符数")
    max_tokens: int = Field(default=8192, description="AI 生成的最大 token 数")
    generation_timeout: float = Field(default=180.0, description="单次生成超时时间（秒，推荐 120-300 秒）")

    # 💾 缓存
    cache_ttl: int = Field(default=300, description="日程缓存有效期（秒，默认 5 分钟）")
    cache_max_size: int = Field(default=100, description="缓存最大条目数（LRU 策略）")

    # ⏰ 定时自动生成
    auto_schedule_enabled: bool = Field(default=True, description="每天定时自动生成日程")
    auto_schedule_time: str = Field(default="00:30", description="自动生成时间（HH:MM 格式）")
    timezone: str = Field(default="Asia/Shanghai", description="时区设置（如 Asia/Shanghai、UTC 等）")

    # 🔐 权限
    admin_users: List[str] = Field(
        default_factory=list,
        description="管理员 QQ 号列表（如 [\"12345\", \"67890\"]，留空则所有人可用）",
    )

    # 🚀 LLM 任务名（v4 新增：主程序 model_config 中需配置的任务名）
    llm_task_name: str = Field(
        default="replyer",
        description="日程生成使用的 LLM 任务名，需在主程序 model_config.toml 中预先配置",
    )

    # 智能注入子段
    inject: InjectConfig = Field(default_factory=InjectConfig)


class AutonomousPlanningConfig(PluginConfigBase):
    """自主规划总配置段：[autonomous_planning]"""

    __ui_label__ = "自主规划"
    __ui_icon__ = "target"
    __ui_order__ = 1

    cleanup_interval: int = Field(default=3600, description="清理间隔（秒）")
    cleanup_old_goals_days: int = Field(default=30, description="保留历史记录天数")

    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)


class AutonomousPlanningV4Config(PluginConfigBase):
    """v4 顶层配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    autonomous_planning: AutonomousPlanningConfig = Field(default_factory=AutonomousPlanningConfig)
