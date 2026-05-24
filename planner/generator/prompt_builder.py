"""Prompt Builder Module.

This module provides prompt building functionality for schedule generation.
Separated from BaseScheduleGenerator to follow Single Responsibility Principle.

v4 改造：
    - 删除 ``config_api.get_global_config`` 直连
    - 全局配置（personality / bot.nickname 等）由 ``config`` 字典中的 ``bot_profile`` 段提供
    - ``bot_profile`` 由 ``ToolsService`` 在构造 ScheduleGenerator 前从插件 ctx 拉取
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
import logging

from ...utils.timezone_manager import TimezoneManager

logger = logging.getLogger(__name__)


# 全局配置默认值（与 v3 等价）
_DEFAULT_PERSONALITY = "是一个女大学生"
_DEFAULT_REPLY_STYLE = ""
_DEFAULT_INTEREST = ""
_DEFAULT_BOT_NAME = "麦麦"


class PromptBuilder:
    """提示词构建器 - 单一职责：构建 LLM 提示词

    该类负责：
        1. 构建初始日程生成提示词
        2. 构建带反馈的重试提示词
        3. 整合配置、上下文、Schema 约束

    v4 改造：
        - 全局配置通过 ``config["bot_profile"]`` 字典传入（由插件层在 ``on_load``
          时一次性拉取并缓存），不再运行时调用 ``config_api``
    """

    def __init__(self, config: Dict[str, Any], tz_manager: TimezoneManager):
        """初始化提示词构建器。

        Args:
            config: 配置字典，期望含 ``bot_profile`` 子段：
                ``{"personality": str, "reply_style": str, "interest": str, "bot_name": str}``
            tz_manager: 时区管理器（用于获取当前时间）
        """
        self.config = config
        self.tz_manager = tz_manager

    def _get_cached_config(self) -> tuple[str, str, str, str]:
        """读取由插件层注入的 bot 配置（无 IO，纯字典访问）。

        Returns:
            ``(personality, reply_style, interest, bot_name)`` 元组
        """
        bot_profile: Dict[str, Any] = self.config.get("bot_profile", {}) or {}
        personality = str(bot_profile.get("personality") or _DEFAULT_PERSONALITY)
        reply_style = str(bot_profile.get("reply_style") or _DEFAULT_REPLY_STYLE)
        interest = str(bot_profile.get("interest") or _DEFAULT_INTEREST)
        bot_name = str(bot_profile.get("bot_name") or _DEFAULT_BOT_NAME)
        return personality, reply_style, interest, bot_name

    def build_schedule_prompt(
        self,
        schedule_type: str,
        preferences: Dict[str, Any],
        schema: Optional[Dict] = None,
        yesterday_context: Optional[str] = None
    ) -> str:
        """构建日程生成提示词（精简版）

        Args:
            schedule_type: 日程类型（daily/weekly/monthly）
            preferences: 用户偏好
            schema: JSON Schema（可选）
            yesterday_context: 昨日上下文（可选）

        Returns:
            完整的提示词字符串
        """
        # 使用缓存的全局配置
        personality, reply_style, interest, bot_name = self._get_cached_config()

        # 从配置读取生成参数
        min_activities = self.config.get('min_activities', 8)
        max_activities = self.config.get('max_activities', 15)
        enable_detailed_description = self.config.get('enable_detailed_description', True)
        min_desc_len = self.config.get('min_description_length', 20)
        max_desc_len = self.config.get('max_description_length', 50)

        # 读取自定义prompt配置
        custom_prompt = self.config.get('custom_prompt', '').strip()

        # 使用时区管理器获取时间信息
        today = self.tz_manager.get_now()
        date_str = today.strftime("%Y-%m-%d")
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[today.weekday()]
        is_weekend = today.weekday() >= 5

        # 昨日上下文
        yesterday_text = yesterday_context or "昨天普通的一天"

        # 核心提示词（精简版）
        prompt = f"""你是{bot_name}，{personality}

今天是{date_str} {weekday}{"（周末）" if is_weekend else ""}
昨天: {yesterday_text}
"""

        # 添加自定义prompt（如果配置了）
        if custom_prompt:
            prompt += f"""
【特殊要求】
{custom_prompt}
"""

        # 根据配置决定描述要求
        if enable_detailed_description:
            desc_requirement = f"2. 每个description {min_desc_len}-{max_desc_len}字，用自然叙述风格（像日记）"
        else:
            desc_requirement = "2. description字段填空字符串\"\"即可（不需要描述）"

        prompt += f"""
【任务】生成今天的详细日程JSON：
🔴 核心要求：日程必须全天无缝衔接，不允许任何时间空档！
   - 每个活动的结束时间 = 下一个活动的开始时间
   - 计算公式：结束时间 = time_slot + duration_hours

1. {min_activities}-{max_activities}个活动，完整覆盖全天（00:00-24:00，无缝衔接）
{desc_requirement}
3. 体现人设：{personality[:50]}...
4. 兴趣相关：{interest if interest else "日常生活"}
5. 表达风格：{reply_style[:30] if reply_style else "自然随意"}
"""

        # 如果有自定义prompt，强调一下
        if custom_prompt:
            prompt += f"6. ⚠️ 优先满足上述【特殊要求】的内容\n"

        prompt += """
【活动类型】
daily_routine(作息)|meal(吃饭)|study(学习)|entertainment(娱乐)|social_maintenance(社交)|exercise(运动)|learn_topic(兴趣)|custom(其他)

⚠️ **重要：meal类型活动命名规范**
- 活动名称必须直接使用：早餐、午餐、晚餐
- 禁止使用：准备xx、零食时间、下午茶等变体
- 时间要求：早餐06:00-09:00，午餐11:00-14:00，晚餐17:00-20:00

【JSON格式示例】（完整展示全天无缝衔接）
{
  "schedule_items": [
    {"name":"睡觉","description":""" + ('"蜷在被窝里睡得很香"' if enable_detailed_description else '""') + ""","goal_type":"daily_routine","priority":"high","time_slot":"00:00","duration_hours":7.5},
    {"name":"起床洗漱","description":""" + ('"迷迷糊糊爬起来刷牙洗脸"' if enable_detailed_description else '""') + ""","goal_type":"daily_routine","priority":"medium","time_slot":"07:30","duration_hours":0.5},
    {"name":"早餐","description":""" + ('"简单吃了点东西"' if enable_detailed_description else '""') + ""","goal_type":"meal","priority":"high","time_slot":"08:00","duration_hours":0.5},
    {"name":"上午学习","description":""" + ('"认真看书学习新知识"' if enable_detailed_description else '""') + ""","goal_type":"study","priority":"high","time_slot":"08:30","duration_hours":3.5},
    {"name":"午餐","description":""" + ('"吃了喜欢的菜"' if enable_detailed_description else '""') + ""","goal_type":"meal","priority":"high","time_slot":"12:00","duration_hours":0.5},
    {"name":"午休","description":""" + ('"小憩一会儿恢复精力"' if enable_detailed_description else '""') + ""","goal_type":"daily_routine","priority":"medium","time_slot":"12:30","duration_hours":0.5},
    {"name":"下午学习","description":""" + ('"继续努力完成学习任务"' if enable_detailed_description else '""') + ""","goal_type":"study","priority":"high","time_slot":"13:00","duration_hours":2.0},
    {"name":"兴趣活动","description":""" + ('"做自己喜欢的事情"' if enable_detailed_description else '""') + ""","goal_type":"learn_topic","priority":"medium","time_slot":"15:00","duration_hours":2.0},
    {"name":"运动","description":""" + ('"出去跑步锻炼身体"' if enable_detailed_description else '""') + ""","goal_type":"exercise","priority":"medium","time_slot":"17:00","duration_hours":1.0},
    {"name":"晚餐","description":""" + ('"吃了丰盛的晚餐"' if enable_detailed_description else '""') + ""","goal_type":"meal","priority":"high","time_slot":"18:00","duration_hours":0.5},
    {"name":"娱乐","description":""" + ('"看视频放松一下"' if enable_detailed_description else '""') + ""","goal_type":"entertainment","priority":"low","time_slot":"18:30","duration_hours":3.0},
    {"name":"夜聊","description":""" + ('"和朋友聊天分享日常"' if enable_detailed_description else '""') + ""","goal_type":"social_maintenance","priority":"medium","time_slot":"21:30","duration_hours":1.0},
    {"name":"睡前准备","description":""" + ('"洗澡护肤准备睡觉"' if enable_detailed_description else '""') + ""","goal_type":"daily_routine","priority":"medium","time_slot":"22:30","duration_hours":1.5}
"""

        prompt += f"""（根据实际情况生成{min_activities}-{max_activities}个活动）
  ]
}}

⚠️ 重要：上面示例展示了全天无缝衔接的正确方式！
- 睡觉 00:00 + 7.5h = 07:30 → 起床洗漱 07:30 ✅ 无缝
- 起床洗漱 07:30 + 0.5h = 08:00 → 早餐 08:00 ✅ 无缝
- 早餐 08:00 + 0.5h = 08:30 → 上午学习 08:30 ✅ 无缝
... (以此类推，每个活动结束时间 = 下个活动开始时间)
- 睡前准备 22:30 + 1.5h = 24:00 (00:00) ✅ 回到起点，完整覆盖全天

⚠️ duration_hours 是活动持续时长（小时），不是重复间隔！

【时间合理性要求 - 重要！】
⚠️ 必须同时满足以下两点：
1. 无缝覆盖全天：每个活动结束时间 = 下个活动开始时间（不允许任何空档）
2. 遵守常识性时间安排，参考以下时间框架：
   • 00:00-07:00  睡觉 (7-8小时)
   • 07:00-08:00  起床/洗漱
   • 08:00-08:30  早餐 ← 必须在 06:00-09:00
   • 08:30-12:00  上午活动（学习/工作/娱乐）
   • 12:00-13:00  午餐+午休 ← 午餐必须在 11:00-14:00
   • 13:00-18:00  下午活动（可细分为2-3个不同活动，避免单个活动超过3小时）
   • 18:00-19:00  晚餐+休息 ← 晚餐必须在 17:00-20:00
   • 19:00-22:00  晚间活动（娱乐/社交/兴趣）
   • 22:00-00:00  睡前准备+早睡 → 回到 00:00

⚠️ 注意：下午和晚间的大时段应该细分成多个活动，不要一个活动占据5小时以上！

【要求】
- 严格JSON格式，无注释
- time_slot按时间递增（HH:MM格式）
- 🔴 核心要求：必须无缝覆盖全天，不能有任何时间空档！
  * 每个活动结束时间 = 下个活动开始时间
  * 计算方式：结束时间 = time_slot + duration_hours
  * 示例：如果活动A在15:00结束，活动B必须从15:00开始！
- ⚠️ 关键活动时间必须合理：早餐6-9点、午餐11-14点、晚餐17-20点、睡觉从22-2点开始
""" + ("- description填空字符串\"\"即可\n" if not enable_detailed_description else f"- description简洁，{min_desc_len}-{max_desc_len}字\n") + f"""- 体现{weekday}特色（{"周末睡懒觉" if is_weekend else "工作日早起"}）
"""

        # 添加Schema约束（精简版）
        if schema:
            import json
            schema_desc = f"""
【Schema要求】
- {min_activities}-{max_activities}个活动（必须）
- 必填：name(2-20字), time_slot, goal_type, priority
""" + ("- description填空字符串\"\"即可\n" if not enable_detailed_description else f"- description: {min_desc_len}-{max_desc_len}字\n") + f"""- priority: high/medium/low
- duration_hours: 0.25-12（活动持续时长，小时）

Schema: {json.dumps(schema.get('properties', {}).get('schedule_items', {}), ensure_ascii=False)}
"""
            prompt += schema_desc

        return prompt

    def build_retry_prompt(
        self,
        schedule_type: str,
        preferences: Dict[str, Any],
        schema: Dict,
        previous_issues: List[str],
        yesterday_context: Optional[str] = None
    ) -> str:
        """构建第二轮prompt（附带反馈）

        Args:
            schedule_type: 日程类型
            preferences: 用户偏好
            schema: JSON Schema
            previous_issues: 上一轮的问题列表
            yesterday_context: 昨日上下文（可选）

        Returns:
            改进后的提示词
        """
        base_prompt = self.build_schedule_prompt(
            schedule_type, preferences, schema, yesterday_context
        )

        feedback = "\n\n⚠️ **上一次生成存在以下问题，请改进：**\n\n"
        for idx, issue in enumerate(previous_issues[:5], 1):  # 只列出前5个
            feedback += f"{idx}. {issue}\n"

        feedback += "\n**请重新生成一个更合理的日程，特别注意以上问题！**\n"

        return base_prompt + feedback
