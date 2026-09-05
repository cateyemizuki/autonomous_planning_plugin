"""自主规划插件 v4 - 配置模型（v4.6.0）。

**v4.6.0 变更**：
    - 作息语义重构：``day_start_time`` / ``day_end_time`` 更名为 ``wake_time`` /
      ``sleep_time``，语义改为"起床（睡醒）时间 / 入睡时间"；
    - 移除从未生效的 ``auto_generate``、已弃用的 ``inject_mode``；
    - 移除实验功能"次日策略推断"整体；
    - 新增 ``list_draw_image``（/plan list 是否绘制图片）与
      ``image_timeout_seconds``（绘制超时，超时静默降级为文本）；
    - 角色裁判的 description 补充完整机制说明；
    - 新增「管理与日志」section：管理员 QQ、会话白名单、LLM 日志控制从 schedule 段迁出；
    - 新增「主动行为」section：原 schedule 段的主动行为配置迁入并扩展——
      白名单拆分为"群聊（直接填群号，留空=所有群聊）"与"其他会话
      （qq:private:xxx / session:xxx）"两项，新增早间问好及其激活开关、
      触发窗口分钟数；活动切换主动发起不再覆盖当天第一个活动、
      睡眠时段（含无睡眠模式）不触发。

旧配置（v4.5.0 及以前）由 ``plugin.py`` 的 ``normalize_plugin_config`` 自动迁移：
    day_start_time → sleep_time（旧语义"日程开始=入睡时刻"与新语义一致映射）
    day_end_time   → wake_time
    schedule.{admin_users, allowed_streams, llm_log_*} → [admin] 段
    schedule.proactive_streams → [proactive] proactive_other_streams
    schedule.{enable_proactive_trigger, enable_frequency_modulation} → [proactive] 段

每个字段都通过 ``json_schema_extra`` 提供 UI 元数据（label / hint / order /
placeholder / rows / item_type 等），WebUI 据此渲染表单；
``description`` 保留长说明用于 schema 文档场景。

字段编排按 ``order`` 数字分组（schedule 段）：
    1-9    作息（起床 / 入睡 / 无睡眠模式）
    10-19  注入开关
    20-39  日程生成参数
    40-49  定时生成
    50-59  跨群上下文 + 跨天活动
    60-69  LLM 任务
    70-79  角色裁判
    80-89  缓存
    90-99  命令展示
    100+   时区
"""

from __future__ import annotations

from typing import ClassVar, List

from maibot_sdk import Field, PluginConfigBase

# 配置版本（config_version）：与 _manifest.json 的 version 保持同步（v4.6.0）。
SUPPORTED_CONFIG_VERSION = "4.6.0"


# ============================================================
# [plugin]
# ============================================================


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置段"""

    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件。关闭后所有日程生成 / 注入 / 命令响应停止。",
        json_schema_extra={
            "label": "启用插件",
            "hint": "关闭后日程生成、注入和命令响应都停止",
            "order": 1,
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置文件版本号",
        json_schema_extra={
            "label": "配置版本",
            "hint": "请勿手动改",
            "disabled": True,
            "order": 2,
        },
    )


# ============================================================
# [autonomous_planning] —— 后台清理参数
# ============================================================


class AutonomousPlanningConfig(PluginConfigBase):
    """自主规划全局后台参数段"""

    __ui_label__: ClassVar[str] = "后台清理"
    __ui_icon__: ClassVar[str] = "trash-2"
    __ui_order__: ClassVar[int] = 1

    cleanup_interval: int = Field(
        default=3600,
        ge=60,
        description="后台清理循环间隔（秒，默认 1 小时）。",
        json_schema_extra={
            "label": "清理间隔",
            "hint": "秒；后台清理过期日程 / 旧目标 / LLM 日志的间隔",
            "order": 1,
        },
    )
    cleanup_old_goals_days: int = Field(
        default=30,
        ge=1,
        description="保留多少天前的已完成 / 取消目标。",
        json_schema_extra={
            "label": "保留天数",
            "hint": "天；超过此天数的旧目标（完成/取消）自动删除",
            "order": 2,
        },
    )


# ============================================================
# [schedule] —— 日程管理（核心段）
# ============================================================


class ScheduleConfig(PluginConfigBase):
    """日程管理配置段"""

    __ui_label__: ClassVar[str] = "日程管理"
    __ui_icon__: ClassVar[str] = "calendar"
    __ui_order__: ClassVar[int] = 2

    # ── 1-9 作息 ─────────────────────────────────────────────

    wake_time: str = Field(
        default="07:00",
        description="每天睡醒起床的时间（HH:MM）。清醒时段的活动从这一刻开始安排，"
                    "是全天日程的作息锚点之一。与入睡时间共同划定作息：起床 07:00 + 入睡 23:00 "
                    "即普通作息；入睡时间早于起床时间（如入睡 03:00 / 起床 11:00）表示跨午夜的夜猫子作息。",
        json_schema_extra={
            "label": "起床（睡醒）时间",
            "hint": "HH:MM；清醒活动从这一刻开始排（默认 07:00）",
            "placeholder": "07:00",
            "order": 1,
        },
    )
    sleep_time: str = Field(
        default="23:00",
        description="每天上床入睡的时间（HH:MM）。这一刻之后到次日起床是睡眠时段：正常模式下"
                    "安排睡觉活动；无睡眠模式下改为'无所事事'，且主动行为不会在睡眠时段触发。"
                    "入睡时间早于起床时间表示跨午夜作息。",
        json_schema_extra={
            "label": "入睡时间",
            "hint": "HH:MM；这一刻之后进入睡眠时段（默认 23:00）",
            "placeholder": "23:00",
            "order": 2,
        },
    )
    no_sleep_mode: bool = Field(
        default=False,
        description="无睡眠模式。开启后生成日程时不安排任何睡眠类活动（睡觉/午休/小憩/打盹等），"
                    "入睡到起床的时段改为'无所事事'（自由活动 / 放空）；提示词与代码后处理双重保证，"
                    "且睡眠时段内主动行为（活动切换发起 / 早间问好）不会触发。"
                    "适合不需要睡眠的角色设定（机器人、AI、非人生物等）。",
        json_schema_extra={
            "label": "无睡眠模式",
            "hint": "开启后睡眠时段改为无所事事，全天不出现睡眠类活动",
            "order": 3,
        },
    )

    # ── 10-19 注入开关 ───────────────────────────────────────

    inject_schedule: bool = Field(
        default=True,
        description="在 planner 决策时把当前活动注入 messages 列表（影响是否回复 / 用哪个工具）。",
        json_schema_extra={
            "label": "注入到 planner",
            "hint": "决策阶段注入；让模型知道当前活动",
            "order": 10,
        },
    )
    inject_into_replyer: bool = Field(
        default=True,
        description="在 replyer 调 LLM 前把当前活动作为 extra_prompt 注入；让回复语气贴合当前状态。"
                    "与 planner 注入共享冷却，不会两阶段连刷。",
        json_schema_extra={
            "label": "注入到 replyer",
            "hint": "回复阶段注入；让回复语气贴合当前活动状态",
            "order": 11,
        },
    )
    max_future_activities: int = Field(
        default=3,
        ge=0,
        description="智能注入时最多显示的未来活动数量。",
        json_schema_extra={
            "label": "未来活动条数",
            "hint": "0 = 不显示；注入文本中'接下来'的活动数量",
            "order": 12,
        },
    )

    # ── 20-39 生成参数 ───────────────────────────────────────

    custom_prompt: str = Field(
        default="",
        description="自定义日程生成提示词。语义：角色的长期生活状态/持续阶段（如\"正在环游世界\"\"备考研究生\"），"
                    "日程生成会延续这一方向（留空使用默认风格）。",
        json_schema_extra={
            "label": "当前生活阶段 / 长期状态",
            "hint": "留空使用默认；如\"正在环游世界\"\"备考研究生\"",
            "rows": 3,
            "placeholder": "（留空使用默认风格）",
            "order": 20,
        },
    )
    use_multi_round: bool = Field(
        default=True,
        description="启用多轮生成（首轮质量不达标时按反馈重试，提升日程质量）。",
        json_schema_extra={
            "label": "多轮生成",
            "hint": "首轮质量不达标时按反馈重试，提升质量",
            "order": 21,
        },
    )
    max_rounds: int = Field(
        default=2,
        ge=1,
        le=5,
        description="多轮生成最大轮数（1-3 推荐 2）。",
        json_schema_extra={
            "label": "最大轮数",
            "hint": "1-5；推荐 2；越多越费 token",
            "order": 22,
        },
    )
    quality_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="多轮生成质量阈值（达到此分即停止），0.80-0.90 推荐 0.85。",
        json_schema_extra={
            "label": "质量阈值",
            "hint": "0-1；达到此分提前结束多轮重试，推荐 0.85",
            "order": 23,
        },
    )
    min_activities: int = Field(
        default=8,
        ge=1,
        description="单日最少活动数（建议 8-10）。",
        json_schema_extra={
            "label": "最少活动数",
            "hint": "单日最少；建议 8-10",
            "order": 24,
        },
    )
    max_activities: int = Field(
        default=15,
        ge=1,
        description="单日最多活动数（建议 12-15）。",
        json_schema_extra={
            "label": "最多活动数",
            "hint": "单日最多；建议 12-15",
            "order": 25,
        },
    )
    enable_detailed_description: bool = Field(
        default=True,
        description="启用详细活动描述（关闭后生成、注入、命令展示都不显示长描述）。",
        json_schema_extra={
            "label": "详细描述",
            "hint": "关闭后只显示活动名，不显示长描述",
            "order": 26,
        },
    )
    min_description_length: int = Field(
        default=20,
        ge=5,
        description="活动描述最小字符数。",
        json_schema_extra={
            "label": "描述最少字数",
            "hint": "20-30 较合理；过短易模糊",
            "order": 27,
        },
    )
    max_description_length: int = Field(
        default=50,
        ge=5,
        description="活动描述最大字符数。",
        json_schema_extra={
            "label": "描述最多字数",
            "hint": "50-100 较合理；过长占 token",
            "order": 28,
        },
    )
    max_tokens: int = Field(
        default=8192,
        ge=1000,
        description="LLM 生成日程的最大 token 数。",
        json_schema_extra={
            "label": "最大 token",
            "hint": "建议 8192；过小可能截断日程",
            "order": 29,
        },
    )
    generation_timeout: float = Field(
        default=180.0,
        ge=10.0,
        description="单次生成超时时间（秒），推荐 120-300。传给 SDK RPC 层，避免被默认 30 秒超时截断。",
        json_schema_extra={
            "label": "生成超时",
            "hint": "秒；推荐 120-300；慢模型可调大",
            "order": 30,
        },
    )
    recent_schedule_days: int = Field(
        default=3,
        ge=1,
        le=14,
        description="生成新日程时回看最近 N 天的日程作为去重参考。N=1 = 只看昨天（旧行为）；N=3-5 推荐。",
        json_schema_extra={
            "label": "回看天数",
            "hint": "1-14；防止交替式重复（周一审稿/周二写专栏/周三又审稿）；推荐 3",
            "order": 31,
        },
    )

    # ── 40-49 定时生成 ────────────────────────────────────────

    auto_schedule_enabled: bool = Field(
        default=True,
        description="每天定时自动生成当天日程（到点且当天无日程时才生成）。",
        json_schema_extra={
            "label": "定时自动生成",
            "hint": "每天到点自动调 LLM 生成当天日程",
            "order": 40,
        },
    )
    auto_schedule_time: str = Field(
        default="00:30",
        description="定时自动生成时间（24 小时制 HH:MM）。若当天已有日程则跳过。",
        json_schema_extra={
            "label": "生成时间",
            "hint": "HH:MM；推荐 00:30 凌晨低峰期",
            "placeholder": "00:30",
            "order": 41,
        },
    )

    # ── 50-59 跨群上下文 + 跨天活动 ────────────────────────────

    history_message_limit: int = Field(
        default=30,
        ge=0,
        description="生成日程时从白名单群里回读最近 N 条聊天消息作为上下文。0 = 禁用。",
        json_schema_extra={
            "label": "历史消息条数",
            "hint": "0 = 禁用；建议 20-50；让日程贴近近期发生的事",
            "order": 50,
        },
    )
    knowledge_search_limit: int = Field(
        default=5,
        ge=0,
        description="生成日程时检索知识库的最大条数。0 = 禁用。",
        json_schema_extra={
            "label": "知识库条数",
            "hint": "0 = 禁用；建议 3-8；让日程参考长期记忆",
            "order": 51,
        },
    )
    cross_day_activity: bool = Field(
        default=True,
        description="启用跨天活动（time_window 跨夜的写法 + 注入时回读昨日跨夜活动）。",
        json_schema_extra={
            "label": "跨天活动",
            "hint": "支持跨夜活动写法；凌晨注入也能识别延续活动",
            "order": 52,
        },
    )

    # ── 60-69 LLM 任务 ────────────────────────────────────────

    llm_task_name: str = Field(
        default="replyer",
        description="日程生成使用的 LLM 任务名，需在主程序 model_config.toml 中预先配置。",
        json_schema_extra={
            "label": "LLM 任务名",
            "hint": "对应主程序 model_config 的 task 名（replyer / planner ...）",
            "placeholder": "replyer",
            "order": 60,
        },
    )

    # ── 70-79 角色裁判 ────────────────────────────────────────

    role_judge_enabled: bool = Field(
        default=True,
        description=(
            "裁判对象与机制：当用户通过 update_schedule_v4 工具提出自然语言日程请求（如"
            "'明天下午一起逛街'、'今晚别学了陪我打游戏'）时，插件把【角色人设 + 今天日期星期 + "
            "当前完整日程 + 用户请求原文】交给 LLM，让它扮演 bot 这个角色判断'我接不接这个请求'，"
            "输出三选一：today=接受并调整今天日程（自动删除今日日程、把请求融入后重生成）；"
            "future=接受未来某天的预约（写入候选清单，到了那天生成日程时自动纳入，到点还会主动兑现）；"
            "reject=角色拒绝（日程保持不变，附角色口吻的理由）。LLM 判定失败或格式异常时安全降级为"
            "'日程不变'。关闭本开关则跳过裁判，所有请求一律记为未来约定。"
        ),
        json_schema_extra={
            "label": "角色裁判模式",
            "hint": "对用户的日程请求做裁决：today 改今天 / future 记预约 / reject 拒绝；关闭后一律记为预约",
            "order": 70,
        },
    )
    role_judge_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="角色裁判 LLM 温度。越低越保守（更倾向拒绝离谱请求）。",
        json_schema_extra={
            "label": "裁判温度",
            "hint": "0-2；建议 0.3；越低越保守、越倾向严格判断",
            "order": 71,
        },
    )

    # ── 80-89 缓存 ────────────────────────────────────────────

    cache_ttl: int = Field(
        default=300,
        ge=10,
        description="日程缓存有效期（秒，默认 5 分钟）；过期后重查 goals 表。",
        json_schema_extra={
            "label": "缓存有效期",
            "hint": "秒；默认 300（5 分钟）",
            "order": 80,
        },
    )
    cache_max_size: int = Field(
        default=100,
        ge=10,
        description="缓存最大条目数（LRU 策略，超出按最近最少使用淘汰）。",
        json_schema_extra={
            "label": "缓存最大条数",
            "hint": "LRU 淘汰；多群可适当调大",
            "order": 81,
        },
    )

    # ── 90-99 命令展示 ────────────────────────────────────────

    list_draw_image: bool = Field(
        default=True,
        description="/plan list 是否绘制日程图片。关闭后 /plan list 只返回文字长文本。",
        json_schema_extra={
            "label": "list 绘制图片",
            "hint": "开启后 /plan list 以'图片+文字'合并转发；关闭只发文字",
            "order": 90,
        },
    )
    image_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        description="日程图片绘制超时（秒）。绘制耗时超过该值时静默放弃图片，"
                    "/plan list 自动降级为纯文字合并转发（不会提示绘制失败）。",
        json_schema_extra={
            "label": "绘制超时",
            "hint": "秒；超时静默放弃图片、只发文字",
            "order": 91,
        },
    )

    # ── 100+ 时区 ─────────────────────────────────────────────

    timezone: str = Field(
        default="Asia/Shanghai",
        description="时区设置（IANA 命名，如 Asia/Shanghai、UTC、America/New_York）。",
        json_schema_extra={
            "label": "时区",
            "hint": "IANA 命名；服务器跨时区时务必显式设置",
            "placeholder": "Asia/Shanghai",
            "order": 100,
        },
    )


# ============================================================
# [inject] —— 智能注入策略
# ============================================================


class InjectConfig(PluginConfigBase):
    """智能注入配置段：``[inject]``。"""

    __ui_label__: ClassVar[str] = "智能注入策略"
    __ui_icon__: ClassVar[str] = "zap"
    __ui_order__: ClassVar[int] = 3

    enable_intent_classification: bool = Field(
        default=True,
        description="启用意图分类（识别用户消息属于问当前 / 问未来 / 闲聊 / 技术问答等，"
                    "决定注入的详细程度与是否注入）。",
        json_schema_extra={
            "label": "启用意图分类",
            "hint": "识别用户消息意图，控制注入详略与时机",
            "order": 1,
        },
    )
    enable_state_analysis: bool = Field(
        default=True,
        description="启用活动状态分析：按活动进度生成情绪化短语，"
                    "如\"学了一会儿了，还算专注\"，并注入到 planner/replyer 提示词。",
        json_schema_extra={
            "label": "启用状态分析",
            "hint": "生成情绪化活动描述，让回复语气更贴合阶段",
            "order": 2,
        },
    )
    enable_inject_optimization: bool = Field(
        default=True,
        description="启用注入优化器（防止重复注入和无效打扰，planner / replyer 共用冷却）。",
        json_schema_extra={
            "label": "启用注入优化",
            "hint": "冷却控制；planner 与 replyer 共享，避免双阶段连刷",
            "order": 3,
        },
    )
    casual_chat_inject_probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="闲聊场景（非直接询问 / 非技术问答）的注入概率（0-1）。",
        json_schema_extra={
            "label": "闲聊注入概率",
            "hint": "0-1；闲聊场景的随机注入概率，0.5 = 一半概率注入",
            "order": 4,
        },
    )
    context_max_turns: int = Field(
        default=3,
        ge=1,
        description="对话上下文保留轮数（用于判断是否在连续讨论日程话题）。",
        json_schema_extra={
            "label": "上下文轮数",
            "hint": "连续 N 轮对话仍判定为日程话题时持续注入",
            "order": 5,
        },
    )
    context_ttl: int = Field(
        default=600,
        ge=60,
        description="对话上下文过期时间（秒），超时后认为是新会话。",
        json_schema_extra={
            "label": "上下文过期",
            "hint": "秒；超过此时间无新消息则重置上下文",
            "order": 6,
        },
    )


# ============================================================
# [admin] —— 管理与日志
# ============================================================


class AdminConfig(PluginConfigBase):
    """管理与日志配置段：管理员权限、会话白名单、LLM 日志控制。"""

    __ui_label__: ClassVar[str] = "管理与日志"
    __ui_icon__: ClassVar[str] = "admin_panel_settings"
    __ui_order__: ClassVar[int] = 4

    admin_users: List[str] = Field(
        default_factory=list,
        description="管理员 QQ 号列表，控制谁能执行 /plan 命令（list / regenerate / delete / clear 等）。"
                    "留空 = 所有人可用。注意：只管命令权限，不影响自然语言触发的日程变更（那由角色裁判把关）。",
        json_schema_extra={
            "label": "管理员 QQ",
            "hint": '纯数字 QQ 号，例 ["123456"]；留空 = 所有人可用 /plan 命令',
            "item_type": "string",
            "placeholder": '["123456"]',
            "order": 1,
        },
    )
    allowed_streams: List[str] = Field(
        default_factory=list,
        description=(
            "会话白名单：控制日程注入与 /plan 命令在哪些聊天里生效（两个功能共用这一份名单）。"
            "留空 = 所有会话生效。日程注入决定 bot 的回复是否贴合当前活动；"
            "注意不影响定时生成（日程全局生成），主动行为另有独立白名单（见「主动行为」段）。"
        ),
        json_schema_extra={
            "label": "会话白名单（注入+命令）",
            "hint": '日程注入与 /plan 命令的生效范围；留空=全部会话；支持 all / qq:group:123456 / qq:private:789 / session:xxx',
            "item_type": "string",
            "placeholder": '[] 或 ["qq:group:123456"]',
            "order": 2,
        },
    )
    llm_log_enabled: bool = Field(
        default=True,
        description="是否把 LLM 调用归档到 data/llm_logs/，方便排查 prompt / 响应。",
        json_schema_extra={
            "label": "LLM 调用归档",
            "hint": "写入 data/llm_logs/ 便于事后排查",
            "order": 3,
        },
    )
    llm_log_retention_days: int = Field(
        default=7,
        ge=1,
        description="LLM 日志保留天数（cleanup_loop 自动清理过期文件）。",
        json_schema_extra={
            "label": "日志保留天数",
            "hint": "超过此天数的日志自动清理",
            "order": 4,
        },
    )


# ============================================================
# [proactive] —— 主动行为
# ============================================================


class ProactiveConfig(PluginConfigBase):
    """主动行为配置段：活动切换主动发起、早间问好、聊天频率调控。

    白名单分两份：
        - 群聊（``proactive_group_ids``）：直接填群号，留空 = 所有群聊生效；
        - 其他会话（``proactive_other_streams``）：``qq:private:789`` /
          ``session:xxx`` 按原逻辑解析，留空 = 不含其他会话。
    两份名单的并集即主动行为的生效范围（早间问好 / 活动切换发起 / 频率调控共用）。
    """

    __ui_label__: ClassVar[str] = "主动行为"
    __ui_icon__: ClassVar[str] = "notifications_active"
    __ui_order__: ClassVar[int] = 5

    proactive_group_ids: List[str] = Field(
        default_factory=list,
        description="群聊主动行为白名单：直接填群号（如 [\"123456\"]），无需前缀。"
                    "留空 = 所有群聊都生效。与'其他会话白名单'的并集为主动行为总生效范围，"
                    "早间问好 / 活动切换主动发起 / 活动频率调控共用。",
        json_schema_extra={
            "label": "群聊主动行为白名单",
            "hint": '直接填群号，例 ["123456"]；留空 = 所有群聊生效',
            "item_type": "string",
            "placeholder": '["123456"]',
            "order": 1,
        },
    )
    proactive_other_streams: List[str] = Field(
        default_factory=list,
        description="其他会话主动行为白名单：私聊与指定会话，按 qq:private:789 / session:xxx 格式填写，"
                    "留空 = 不包含其他会话（群聊见'群聊主动行为白名单'）。",
        json_schema_extra={
            "label": "其他会话主动行为白名单",
            "hint": 'qq:private:789 / session:xxx；留空 = 不含其他会话',
            "item_type": "string",
            "placeholder": '["qq:private:789"]',
            "order": 2,
        },
    )
    proactive_fresh_window_minutes: int = Field(
        default=10,
        ge=1,
        le=60,
        description="主动行为触发窗口（分钟）。活动开始后，每个会话在窗口内获得一个独立随机延迟，"
                    "延迟结束即触发（轮询粒度约 1 分钟）；错过整个窗口（如停机）则放弃本次触发。"
                    "活动切换主动发起与早间问好共用该窗口。",
        json_schema_extra={
            "label": "触发窗口（分钟）",
            "hint": "活动开始后 N 分钟内触发；每个会话独立随机延迟（默认 10）",
            "order": 3,
        },
    )
    enable_proactive_trigger: bool = Field(
        default=True,
        description="活动切换主动发起：活动开始后在触发窗口内让 bot 主动开口（每群每活动每天 1 次）。"
                    "不覆盖当天睡醒后的第一个活动（该活动由'早间问好'负责），"
                    "设定的睡眠时段内（含无睡眠模式）也不会触发。",
        json_schema_extra={
            "label": "活动切换主动发起",
            "hint": "活动开始后窗口内随机时机开口；不覆盖首个活动、睡眠时段不触发",
            "order": 4,
        },
    )
    enable_morning_greeting: bool = Field(
        default=False,
        description="早间问好：bot 睡醒后第一个活动开始时，主动向白名单内的会话道早安。"
                    "触发方式与活动切换主动发起相同（触发窗口内每会话独立随机延迟，"
                    "错过窗口不补发）。",
        json_schema_extra={
            "label": "早间问好",
            "hint": "睡醒后第一个活动开始时向白名单会话问早安",
            "order": 5,
        },
    )
    morning_greeting_require_activation: bool = Field(
        default=False,
        description="早间问好是否需要群内激活。开启后：随机延迟结束并不会立即问好——"
                    "从 bot 睡醒起算观察会话消息，期间有人说话则照常问好；"
                    "截止到第一个活动结束前 10 分钟仍无人说话，放弃该会话的早间问好。",
        json_schema_extra={
            "label": "早间问好需激活",
            "hint": "开启后群里有人说话才问好；截止第一个活动结束前 10 分钟仍无人则放弃",
            "order": 6,
        },
    )
    enable_frequency_modulation: bool = Field(
        default=True,
        description="按当前活动类型动态调节聊天频率。"
                    "通过 ctx.frequency.set_adjust 推给 heartflow：学习/工作时频率 30%，"
                    "休息/娱乐时 130%~150%。需要白名单内存在生效会话。",
        json_schema_extra={
            "label": "活动频率调控",
            "hint": "学习/工作时降频；休息/娱乐时升频",
            "order": 7,
        },
    )


# ============================================================
# 顶层 —— 全部 section 扁平挂载，SDK 才能展开为 UI section
# ============================================================


class AutonomousPlanningV4Config(PluginConfigBase):
    """v4 顶层配置模型。

    **注意**：所有 PluginConfigBase 子段都必须作为**顶层字段**挂载，
    SDK 才会展开为独立 UI section（嵌套子段会被当成普通对象字段，UI 不渲染）。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    autonomous_planning: AutonomousPlanningConfig = Field(default_factory=AutonomousPlanningConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    inject: InjectConfig = Field(default_factory=InjectConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
