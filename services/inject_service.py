"""日程注入业务实现（HookHandler 入口：``maisaka.planner.before_request``）。

业务子模块（IntentClassifier / ActivityStateAnalyzer / InjectOptimizer /
ContentTemplateEngine / ConversationContextCache）实现于 ``handlers/inject/``。

向 LLM 请求注入：在 ``messages: list[PromptMessage]`` 的第一条 system 消息
之后插入一条新的 system 消息，承载当前日程信息。

v4.2 改造点：
    - 删除 smart/rule 双模式，合并为单管道（IntentClassifier → InjectOptimizer →
      _render_unified_prompt）。``inject_mode`` 配置字段保留仅为向后兼容
    - 按 intent 路由模板复杂度：QUERY_CURRENT ~70 token / QUERY_FUTURE ~90 token /
      CASUAL_CHAT ~25 token，相比 v4.1 smart 平均省 50% 注入 token
    - 当前活动剩余 0~15 分钟时附"约还剩 X 分钟"时间衰减提示
    - replyer 注入文本从 ~100 token 缩到 ~30 token（planner 已提供完整上下文）
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
import asyncio
import logging
import re
import time
from datetime import timedelta

from ..cache.lru_cache import LRUCache
from ..handlers.exception_handler import handle_exception, handle_exception_silent
from ..handlers.inject import UserIntent
from ..planner.goal_manager import get_goal_manager
from ..utils.stream_filter import is_stream_allowed
from ..utils.time_utils import parse_time_window, strip_tz
from ..utils.timezone_manager import TimezoneManager

if TYPE_CHECKING:
    from ..handlers.inject import (
        ActivityState,
        ActivityStateAnalyzer,
        ContentTemplateEngine,
        ConversationContextCache,
        InjectOptimizer,
        IntentClassifier,
    )
    from ..plugin import AutonomousPlanningPluginV4

logger = logging.getLogger(__name__)


# 时间相关关键词（用于传统模式判断是否需要注入日程）
_TIME_KEYWORDS = {
    "现在", "当前", "正在", "在做", "在干",
    "今天", "今日", "今早", "今晚",
    "明天", "昨天", "后天", "前天",
    "几点", "什么时候", "多久", "时间",
    "安排", "计划", "日程", "行程",
    "接下来", "等下", "稍后", "之后",
    "早上", "中午", "下午", "晚上", "夜里",
    "忙", "空闲", "有空", "在忙",
    "做什么", "干什么", "要做",
}

# 预编译正则（一次匹配所有时间关键词）
_TIME_KEYWORDS_PATTERN = re.compile("|".join(_TIME_KEYWORDS))


class InjectService:
    """日程注入服务。"""

    def __init__(self, plugin: "AutonomousPlanningPluginV4") -> None:
        """初始化 InjectService，加载智能注入组件、缓存、配置。

        Args:
            plugin: 当前插件实例
        """
        self._plugin = plugin
        cfg = plugin.config.schedule

        # 缓存：把 TTL 直接传给 LRUCache，由它统一管理过期，不再二次封装
        self._schedule_cache: LRUCache = LRUCache(max_size=cfg.cache_max_size, ttl=cfg.cache_ttl)

        # 时区管理器
        self._tz_manager: TimezoneManager = TimezoneManager(cfg.timezone)

        # 智能注入组件
        self._intent_classifier: Optional[Any] = None
        self._state_analyzer: Optional[Any] = None
        self._content_engine: Optional[Any] = None
        self._inject_optimizer: Optional[Any] = None
        self._context_cache: Optional[Any] = None
        self._activity_state_cls: Optional[Any] = None

        self._load_smart_components()

        logger.debug(
            "InjectService 初始化完成（cache_max=%d, cache_ttl=%d）",
            cfg.cache_max_size,
            cfg.cache_ttl,
        )

    def _load_smart_components(self) -> None:
        """加载智能注入子模块（IntentClassifier / InjectOptimizer / ContextCache）。

        v4.2 起统一管道：smart/rule 双模式合并为单管道，模板路由直接在
        ``_render_unified_prompt`` 内完成，不再走 ``ContentTemplateEngine``。
        ``inject_mode`` 配置字段保留仅为向后兼容，运行时已忽略。
        """
        inj = self._plugin.config.inject
        cfg = self._plugin.config.schedule

        try:
            from ..handlers.inject import (
                ActivityState,
                ActivityStateAnalyzer,
                ContentTemplateEngine,
                ConversationContextCache,
                InjectOptimizer,
                IntentClassifier,
            )

            self._intent_classifier = IntentClassifier() if inj.enable_intent_classification else None
            self._state_analyzer = ActivityStateAnalyzer() if inj.enable_state_analysis else None
            # v4.2: 保留 content_engine 加载（向后兼容），但 _build_inject_text 已不再调用
            self._content_engine = (
                ContentTemplateEngine(self._state_analyzer) if inj.enable_state_analysis else None
            )
            self._inject_optimizer = (
                InjectOptimizer(
                    cache_ttl=cfg.cache_ttl,
                    casual_inject_probability=inj.casual_chat_inject_probability,
                )
                if inj.enable_inject_optimization
                else None
            )
            self._context_cache = ConversationContextCache(
                max_turns=inj.context_max_turns,
                ttl=inj.context_ttl,
            )
            self._activity_state_cls = ActivityState

            logger.debug(
                "✅ 智能注入组件已加载 (intent=%s, optimizer=%s, context=%d/%ds)",
                inj.enable_intent_classification,
                inj.enable_inject_optimization,
                inj.context_max_turns,
                inj.context_ttl,
            )
        except ImportError as exc:
            logger.warning(f"智能注入组件加载失败，退化为兜底管道（仅 tech/command 过滤）: {exc}")
            self._intent_classifier = None
            self._state_analyzer = None
            self._content_engine = None
            self._inject_optimizer = None
            self._context_cache = None
            self._activity_state_cls = None

    # ------------------------------------------------------------
    # 后台缓存预热
    # ------------------------------------------------------------

    @handle_exception_silent("缓存预热失败: {e}", log_level="warning")
    async def preheat_cache(self) -> None:
        """启动后预热缓存（异步任务）。"""
        await asyncio.sleep(5)  # 等待系统初始化
        logger.debug("🔥 开始预热日程缓存...")
        await self._get_current_schedule("global")
        logger.debug("✅ 日程缓存预热完成")

    # ------------------------------------------------------------
    # Hook 主入口
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # Hook 主入口（planner / replyer 两个）
    # ------------------------------------------------------------

    async def inject_into_replyer_extra_prompt(
        self,
        session_id: str = "",
        attempt: int = 1,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """``maisaka.replyer.before_request`` Hook 入口。

        在 replyer 向 LLM 发起回复请求前，把当前活动作为 ``extra_prompt``
        注入。主程序会把 ``extra_prompt`` 拼接到 ``reference_info``，让
        回复模型自然贴合当前状态语气。

        活人感策略：
        - 重试请求（``attempt > 1``）直接跳过，避免重复加压力
        - 与 planner 注入共用 InjectOptimizer 冷却（同会话 + 同活动短时间内不重复）
        - 文本明确写"不要主动提及"，让 LLM 自行判断要不要带出来
        - 白名单 / cross_day_activity 等开关全部复用 planner 注入分支

        Args:
            session_id: 当前会话 ID。
            attempt: 当前回复尝试序号（从 1 开始）；> 1 表示重试。
            **kwargs: 其它 hook 参数（task_name / reference_info / ...），未使用。

        Returns:
            ``{"action": "continue"}`` 或 ``{"action": "continue", "modified_kwargs": {"extra_prompt": "..."}}``。
        """
        del kwargs

        cfg = self._plugin.config.schedule
        if not self._plugin.config.plugin.enabled or not cfg.inject_into_replyer:
            return {"action": "continue"}

        # 重试不重复注入
        if attempt and int(attempt) > 1:
            return {"action": "continue"}

        # 白名单过滤（与 planner 一致）
        if not is_stream_allowed(session_id, cfg.allowed_streams):
            return {"action": "continue"}

        try:
            chat_id = session_id or "global"
            user_id = session_id or "unknown"

            current_activity, current_description, future_activities, _activity_type = (
                await self._get_current_schedule(chat_id)
            )
            if not current_activity:
                return {"action": "continue"}

            # 复用 InjectOptimizer 冷却（与 planner 共享）
            if self._inject_optimizer is not None and self._intent_classifier is not None:
                # replyer 阶段我们没有"用户消息"可以分类意图；用一个中性意图过冷却即可
                neutral_intent = UserIntent.CASUAL_CHAT
                should_inject, skip_reason = self._inject_optimizer.should_inject(
                    user_id, neutral_intent, current_activity, confidence=0.5,
                )
                if not should_inject:
                    logger.debug(f"replyer 注入被冷却跳过: {skip_reason}")
                    return {"action": "continue"}

            extra_prompt = self._build_replyer_extra_prompt(
                current_activity=current_activity,
                description=current_description or "",
                future_activities=future_activities,
                remaining_minutes=self._compute_remaining_minutes(current_activity),
            )

            if self._inject_optimizer is not None and self._intent_classifier is not None:
                self._inject_optimizer.record_injection(
                    user_id, current_activity, extra_prompt, UserIntent.CASUAL_CHAT,
                )

            logger.info(f"✅ replyer 注入: {current_activity}")
            return {
                "action": "continue",
                "modified_kwargs": {"extra_prompt": extra_prompt},
            }

        except Exception as exc:
            logger.error(f"replyer 注入失败: {exc}", exc_info=True)
            return {"action": "continue"}

    def _build_replyer_extra_prompt(
        self,
        current_activity: str,
        description: str,
        future_activities: List[Tuple[str, str]],
        remaining_minutes: Optional[int],
    ) -> str:
        """构建 replyer 极简 extra_prompt（v4.2：30 token 内 + 时间衰减提示）。

        v4.2 改造点：
            - planner 已注入完整上下文（system 消息），replyer 只需要锚定当前状态
            - 文本从 80~100 token 缩到 ~30 token，省 50% 调用成本
            - 仍保留"不要主动提及"指令，避免 LLM 强行转话题

        Args:
            current_activity: 当前活动名。
            description: 活动描述；启用时附在括号中。
            future_activities: 后续活动；只取最近 1 条做锚定。
            remaining_minutes: 当前活动剩余分钟；仅 0~15 分钟时附加。
        """
        cfg = self._plugin.config.schedule
        enable_detailed = cfg.enable_detailed_description

        remaining_hint = ""
        if remaining_minutes is not None and 0 < remaining_minutes <= 15:
            remaining_hint = f"（约还剩 {remaining_minutes} 分钟）"

        if enable_detailed and description:
            state_line = f"你现在正在 {current_activity}（{description}）{remaining_hint}。"
        else:
            state_line = f"你现在正在 {current_activity}{remaining_hint}。"

        lines: List[str] = ["【角色当前状态】", state_line]
        if future_activities:
            time_str, name = future_activities[0]
            lines.append(f"接下来 {time_str} 要 {name}。")
        lines.append("⚠️ 不要主动提及；仅在用户问到 / 强相关时自然带过。")
        return "\n".join(lines)

    async def inject_into_planner_messages(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """``maisaka.planner.before_request`` Hook 入口。

        在 Maisaka 向 LLM 发起规划请求前调用，按当前日程信息构造 system 消息
        并插入到 messages 列表，达到 v3 等价的 prompt 注入效果。

        Args:
            messages: 序列化后的 PromptMessage dict 列表
            session_id: 当前会话 ID
            **kwargs: 其余 hook 参数（tool_definitions / selected_history_count 等）

        Returns:
            ``{"action": "continue"}`` 或 ``{"action": "continue", "modified_kwargs": {"messages": [...]}}``
        """
        del kwargs

        if not messages or not isinstance(messages, list):
            return {"action": "continue"}

        cfg = self._plugin.config.schedule
        if not self._plugin.config.plugin.enabled or not cfg.inject_schedule:
            return {"action": "continue"}

        # 白名单过滤（留空 = 全部允许，向后兼容）
        if not is_stream_allowed(session_id, cfg.allowed_streams):
            logger.debug(f"会话 {session_id} 不在白名单，跳过注入")
            return {"action": "continue"}

        try:
            chat_id = session_id or "global"

            # 提取最后一条 user 消息用于意图分析
            user_message = self._extract_last_user_text(messages)
            user_id = session_id or "unknown"

            # 检查对话上下文：判断是否在连续讨论日程话题
            context_continue_inject = False
            context_reason: Optional[str] = None
            if self._context_cache:
                context_continue_inject, context_reason = self._context_cache.should_continue_inject(
                    user_id, None,
                )

            # 获取当前日程
            current_activity, current_description, all_future_activities, activity_type = await self._get_current_schedule(chat_id)

            # 更新对话上下文：用当前活动重新判断
            if self._context_cache and context_continue_inject:
                context_continue_inject, context_reason = self._context_cache.should_continue_inject(
                    user_id, current_activity,
                )

            # 无当前活动：仅记录上下文，不注入
            if not current_activity:
                if self._context_cache:
                    self._context_cache.add_turn(
                        user_id=user_id,
                        user_message=user_message,
                        intent=None,
                        injected=False,
                        activity=None,
                    )
                return {"action": "continue"}

            # 按模式构造注入文本
            inject_content, injected, detected_intent = self._build_inject_text(
                user_message=user_message,
                current_activity=current_activity,
                current_description=current_description,
                future_activities=all_future_activities,
                activity_type=activity_type,
                context_continue_inject=context_continue_inject,
                context_reason=context_reason,
                user_id=user_id,
            )

            # 记录到上下文缓存
            if self._context_cache:
                self._context_cache.add_turn(
                    user_id=user_id,
                    user_message=user_message,
                    intent=detected_intent,
                    injected=injected,
                    activity=current_activity,
                )

            if not inject_content:
                return {"action": "continue"}

            # 把注入文本作为 system 消息插入 messages 列表
            modified_messages = self._inject_system_message(messages, inject_content)
            return {
                "action": "continue",
                "modified_kwargs": {"messages": modified_messages},
            }

        except Exception as exc:
            logger.error(f"注入日程信息失败: {exc}", exc_info=True)
            return {"action": "continue"}

    # ------------------------------------------------------------
    # 消息提取与插入
    # ------------------------------------------------------------

    @staticmethod
    def _extract_last_user_text(messages: List[Dict[str, Any]]) -> str:
        """从 PromptMessage 列表里提取最新一条 **含文本** 的 user 消息。

        如果最新一条 user 消息只含图片（纯 image part），向上继续找前一条 user
        消息，直至找到含文本的或耗尽。

        Args:
            messages: 序列化后的 PromptMessage dict 列表

        Returns:
            最近一条含文本的 user 消息内容（找不到时返回空串）
        """
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    return content
                continue  # 空文本 → 找上一条 user
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, str) and part.strip():
                        return part
                    if isinstance(part, dict) and "text" in part:
                        text = str(part["text"]).strip()
                        if text:
                            return text
                    # 图片 / 元组形式跳过本 part
                # 当前 user 消息无文本 part → 继续找前一条 user
                continue
            # content 是其他类型 → 继续找
        return ""

    @staticmethod
    def _inject_system_message(
        messages: List[Dict[str, Any]],
        inject_content: str,
    ) -> List[Dict[str, Any]]:
        """把注入文本作为一条 system 消息插入到 messages 列表。

        策略：紧跟在第一条 system 消息之后插入（不破坏原始人设 prompt）；
        若没有 system 消息，则插到列表开头。

        Args:
            messages: 原始消息列表
            inject_content: 要注入的文本

        Returns:
            新的消息列表（深拷贝级别的副本，避免污染调用方）
        """
        injection: Dict[str, Any] = {
            "role": "system",
            "content": inject_content,
        }

        # 找到第一个 system 消息的位置
        first_system_idx: Optional[int] = None
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                first_system_idx = idx
                break

        new_messages = list(messages)  # 浅拷贝列表
        insert_at = (first_system_idx + 1) if first_system_idx is not None else 0
        new_messages.insert(insert_at, injection)
        return new_messages

    # ------------------------------------------------------------
    # 注入文本构建（v4.2 统一管道：IntentClassifier + InjectOptimizer + TemplateRouter）
    # ------------------------------------------------------------

    def _build_inject_text(
        self,
        user_message: str,
        current_activity: str,
        current_description: Optional[str],
        future_activities: List[Tuple[str, str]],
        activity_type: Optional[str],
        context_continue_inject: bool,
        context_reason: Optional[str],
        user_id: str,
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """v4.2 统一注入管道：分类 → 决策 → 模板路由。

        相比 v4.1 的双模式（smart/rule）：
        - 关键词只在 IntentClassifier 维护一处
        - InjectOptimizer 的冷却/低置信度/重复抑制对所有场景生效（smart 模式以前无冷却）
        - 按 intent 选模板复杂度，闲聊场景从 ~150 token 降到 ~30 token

        Args:
            user_id: 实际是会话/聊天流维度的 scope ID（``session_id``），群聊内全员
                共享冷却历史，避免群里多人各注一遍。

        Returns:
            (inject_content, injected, detected_intent) 三元组。``inject_content``
            为 ``None`` 时表示决策跳过；``detected_intent`` 用于上下文缓存追踪。
        """
        del activity_type  # 当前未使用，保留参数兼容未来扩展

        # 1. 意图分类（关键词唯一在 IntentClassifier 维护）
        intent: UserIntent
        confidence: float
        if self._intent_classifier is not None:
            intent, confidence = self._intent_classifier.classify(user_message)
        else:
            # 没装意图分类器时退化为闲聊兜底（让冷却+模板路由依然能跑）
            intent, confidence = UserIntent.CASUAL_CHAT, 0.5

        detected_intent = intent.value

        # 2. 决策（InjectOptimizer 没装时只跳过 tech/command 兜底）
        if context_continue_inject:
            logger.info(f"📖 对话上下文触发注入: {context_reason}")
            should_inject = True
            skip_reason: Optional[str] = None
        elif self._inject_optimizer is not None:
            should_inject, skip_reason = self._inject_optimizer.should_inject(
                user_id, intent, current_activity, confidence,
            )
        else:
            # 优化器缺失时硬过滤 tech/command，其余允许
            if intent in (UserIntent.TECH_QUESTION, UserIntent.COMMAND_EXECUTION):
                should_inject, skip_reason = False, f"{intent.value}场景，跳过注入"
            else:
                should_inject, skip_reason = True, None

        if not should_inject:
            logger.debug(f"注入跳过: intent={detected_intent}, reason={skip_reason}")
            return None, False, detected_intent

        # 3. 时间衰减提示（仅剩 0~15 分钟时显示，让 LLM 感知活动即将切换）
        remaining_minutes = self._compute_remaining_minutes(current_activity)

        # 4. 按 intent 渲染统一模板
        inject_content = self._render_unified_prompt(
            intent=intent,
            current_activity=current_activity,
            current_description=current_description,
            future_activities=future_activities,
            remaining_minutes=remaining_minutes,
            context_continue_inject=context_continue_inject,
            context_reason=context_reason,
        )
        if inject_content is None:
            return None, False, detected_intent

        # 5. 记录注入历史（让 InjectOptimizer 的冷却生效）
        if self._inject_optimizer is not None:
            self._inject_optimizer.record_injection(
                user_id, current_activity, inject_content, intent,
            )

        logger.info(
            f"✅ 注入: intent={detected_intent}, confidence={confidence:.2f}, "
            f"len={len(inject_content)}, remaining={remaining_minutes}"
        )
        return inject_content, True, detected_intent

    def _render_unified_prompt(
        self,
        intent: "UserIntent",
        current_activity: str,
        current_description: Optional[str],
        future_activities: List[Tuple[str, str]],
        remaining_minutes: Optional[int],
        context_continue_inject: bool,
        context_reason: Optional[str],
    ) -> Optional[str]:
        """按 intent 路由统一模板（v4.2 新增）。

        Token 预算（含开关 ``enable_detailed_description`` / ``max_future_activities``）：
            - QUERY_CURRENT  → ~70 token（描述 + 1 条未来 + 剩余时间）
            - QUERY_FUTURE   → ~90 token（多条未来 + 剩余时间）
            - CASUAL_CHAT    → ~25 token（仅"现在 xxx"）
            - GREETING/其他  → ~30 token（minimal + 兜底提示）
            - TECH_QUESTION  → None（理论上 optimizer 已拒，兜底保险）
            - COMMAND        → None
        """
        if intent in (UserIntent.TECH_QUESTION, UserIntent.COMMAND_EXECUTION):
            return None

        cfg = self._plugin.config.schedule
        enable_detailed = cfg.enable_detailed_description
        max_show = cfg.max_future_activities

        # 当前活动 + 时间衰减提示
        remaining_hint = ""
        if remaining_minutes is not None and 0 < remaining_minutes <= 15:
            remaining_hint = f"（约还剩 {remaining_minutes} 分钟）"

        if enable_detailed and current_description:
            current_line = f"现在：{current_activity}（{current_description}）{remaining_hint}"
        else:
            current_line = f"现在：{current_activity}{remaining_hint}"

        lines: List[str] = ["【可选上下文 - Bot 的当前日程】", current_line]

        # 按 intent 决定文本复杂度
        if intent == UserIntent.QUERY_FUTURE and future_activities and max_show > 0:
            # 询问未来 → 展示多条未来活动
            lines.append("接下来的安排:")
            for time_str, name in future_activities[:max_show]:
                lines.append(f"  {time_str} - {name}")
            lines.extend(["", "💡 用户询问未来计划，请自然地介绍后续安排。"])

        elif intent == UserIntent.QUERY_CURRENT:
            # 询问当前 → 展示 1 条未来即可
            if future_activities and max_show > 0:
                time_str, name = future_activities[0]
                lines.append(f"接下来：{time_str} - {name}")
            lines.extend(["", "💡 用户直接询问当前状态，请如实告知当前活动及状态。"])

        else:
            # CASUAL_CHAT / UNKNOWN / 兜底 → minimal
            if context_continue_inject:
                lines.extend(["", f"💡 对话延续中（{context_reason}），可自然带过当前活动。"])
            else:
                lines.extend(["", "💡 仅供参考；不相关请完全忽略，不要刻意提及。"])

        lines.extend(["", "---", ""])
        return "\n".join(lines)

    def _compute_remaining_minutes(self, activity_name: str) -> Optional[int]:
        """估算当前活动剩余多少分钟。

        从 GoalManager 反查活动的 ``time_window``，结合当前时间计算剩余分钟。
        跨夜活动（``end_minutes > 1440``）做归一化。

        Args:
            activity_name: 当前活动名称。

        Returns:
            剩余分钟数；查不到时间窗 / 已结束 / 解析失败时返回 ``None``。
        """
        try:
            goal_manager = get_goal_manager()
            goals = goal_manager.get_active_goals(chat_id="global")
            if not goals:
                return None

            now = self._tz_manager.get_now()
            current_min = now.hour * 60 + now.minute

            for goal in goals:
                if goal.name != activity_name:
                    continue
                tw: Optional[List[int]] = None
                if goal.parameters and "time_window" in goal.parameters:
                    tw = goal.parameters.get("time_window")
                elif goal.conditions:
                    tw = goal.conditions.get("time_window")
                if not tw:
                    continue
                start, end = parse_time_window(tw)
                if start is None or end is None:
                    continue
                # 跨夜归一化：end_minutes > 1440 表示跨夜，凌晨阶段 end 减 1440 即真实结束分钟
                if end > 1440 and current_min < 720:
                    end -= 1440
                remaining = end - current_min
                if remaining <= 0:
                    return None
                return remaining
        except Exception as exc:  # noqa: BLE001
            logger.debug("计算剩余分钟失败: %s", exc)
        return None

    # ------------------------------------------------------------
    # 对外公开：当前活动快照（供 @API 转发，跨插件可调）
    # ------------------------------------------------------------

    async def get_current_activity_snapshot(self, chat_id: str = "global") -> Dict[str, Any]:
        """返回当前时间段最新日程活动的结构化快照。

        与日程注入用的内部查询走同一条缓存路径，结果与日志中
        "✅ Smart 注入: <活动名>" 一致。

        Args:
            chat_id: 业务范围，留空默认 ``global``。

        Returns:
            dict::

                {
                    "has_activity": bool,
                    "activity": None | {
                        "name": str,
                        "description": str,
                        "goal_type": str,
                        "time_window": "HH:MM-HH:MM" | None,
                    },
                    "next_activities": [{"time": "HH:MM", "name": str}, ...],
                    "as_of": ISO8601 字符串,
                    "timezone": str,
                }
        """
        current_activity, current_description, future_activities, activity_type = (
            await self._get_current_schedule(chat_id or "global")
        )

        now = self._tz_manager.get_now()
        activity_payload: Optional[Dict[str, Any]] = None
        if current_activity:
            activity_payload = {
                "name": current_activity,
                "description": current_description or "",
                "goal_type": activity_type or "",
                "time_window": self._lookup_time_window(current_activity, chat_id or "global"),
            }

        return {
            "has_activity": current_activity is not None,
            "activity": activity_payload,
            "next_activities": [{"time": t, "name": n} for t, n in future_activities],
            "as_of": now.isoformat(timespec="seconds"),
            "timezone": self._tz_manager.timezone_str,
            "error": None,  # 成功路径显式给 None，让调用方能统一用 result.get("error") 判断
        }

    def _lookup_time_window(self, activity_name: str, chat_id: str) -> Optional[str]:
        """根据活动名反查时间窗口字符串（HH:MM-HH:MM）。"""
        try:
            from ..planner.goal_manager import get_goal_manager
            from ..utils.time_utils import parse_time_window

            goal_manager = get_goal_manager()
            goals = goal_manager.get_active_goals(chat_id="global")
            if not goals and chat_id and chat_id != "global":
                goals = goal_manager.get_active_goals(chat_id=chat_id)

            for goal in goals:
                if goal.name != activity_name:
                    continue
                tw = None
                if goal.parameters and "time_window" in goal.parameters:
                    tw = goal.parameters.get("time_window")
                elif goal.conditions:
                    tw = goal.conditions.get("time_window")
                if not tw:
                    continue
                start_minutes, end_minutes = parse_time_window(tw)
                if start_minutes is None or end_minutes is None:
                    continue
                start_str = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
                end_minutes_norm = end_minutes if end_minutes < 1440 else end_minutes - 1440
                end_str = f"{end_minutes_norm // 60:02d}:{end_minutes_norm % 60:02d}"
                return f"{start_str}-{end_str}"
        except Exception as exc:  # noqa: BLE001
            logger.debug("反查时间窗口失败: %s", exc)
        return None

    # ------------------------------------------------------------
    # 当前日程查询（带缓存，与 v3 等价）
    # ------------------------------------------------------------

    @handle_exception("获取当前日程信息失败: {e}", log_level="warning", default_return=(None, None, [], None))
    async def _get_current_schedule(
        self,
        chat_id: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], List[Tuple[str, str]], Optional[str]]:
        """获取当前日程信息（带缓存）。

        缓存策略：
            - 缓存键按 ``chat_id + 日期 + 15 分钟时间窗口`` 分桶，
              同一时间窗口内反复查询命中同一 key
            - TTL 由 ``LRUCache`` 自管，统一使用 ``cfg.cache_ttl``（默认 300 秒）
            - 实际生效缓存有效期 = ``min(15 分钟窗口跳动间隔, cache_ttl)``

        Returns:
            ``(当前活动, 活动描述, 所有未来活动列表, 当前活动类型)``
        """
        now = self._tz_manager.get_now()
        current_hour = now.hour
        current_minute = now.minute

        # 15 分钟窗口缓存键
        time_window = (current_hour * 60 + current_minute) // 15
        cache_key = f"{chat_id or 'global'}_{now.strftime('%Y%m%d')}_{time_window}"

        # 命中缓存直接返回
        cached_result = await self._schedule_cache.get(cache_key)
        if cached_result is not None:
            return cached_result

        # 重新查询
        goal_manager = get_goal_manager()
        cross_day_enabled = bool(
            self._plugin.config.schedule.cross_day_activity
        )
        goals = goal_manager.get_active_goals(chat_id="global")
        if not goals and chat_id and chat_id != "global":
            goals = goal_manager.get_active_goals(chat_id=chat_id)

        if not goals:
            result = (None, None, [], None)
            await self._schedule_cache.set(cache_key, result)
            return result

        current_time_minutes = current_hour * 60 + current_minute
        today_date = now.strftime("%Y-%m-%d")
        yesterday_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        scheduled_goals: List[Tuple[Any, List[int], bool, bool]] = []
        for goal in goals:
            # 跳过 pending_commitment（不是日程活动）
            if goal.goal_type == "pending_commitment":
                continue
            tw: Optional[List[int]] = None
            if goal.parameters and "time_window" in goal.parameters:
                tw = goal.parameters.get("time_window")
            elif goal.conditions:
                tw = goal.conditions.get("time_window")

            if tw:
                is_today = False
                is_yesterday = False
                if goal.created_at:
                    if isinstance(goal.created_at, str):
                        is_today = goal.created_at.startswith(today_date)
                        is_yesterday = goal.created_at.startswith(yesterday_date)
                    else:
                        goal_date_str = goal.created_at.strftime("%Y-%m-%d")
                        is_today = goal_date_str == today_date
                        is_yesterday = goal_date_str == yesterday_date

                # 跨夜活动开关关闭时只看今天；开启时也接受昨天创建的跨夜活动
                if is_today or (cross_day_enabled and is_yesterday):
                    scheduled_goals.append((goal, tw, is_today, is_yesterday))

        if not scheduled_goals:
            result = (None, None, [], None)
            await self._schedule_cache.set(cache_key, result)
            return result

        def _get_start_minutes(item: Tuple[Any, List[int], bool, bool]) -> int:
            _, tw, _, _ = item
            if not tw or len(tw) < 2:
                return 0
            start_val = tw[0]
            # 兼容旧格式（小时表示）：end_val > 24 为分钟格式
            return start_val if tw[1] > 24 else start_val * 60

        scheduled_goals.sort(key=_get_start_minutes)

        # 查找当前活动：
        #   今天创建的：直接判 current_time_minutes 是否落在 time_window 内（含跨夜）
        #   昨天创建的：只有 time_window 跨夜（end_minutes > 1440）才可能延续到今天凌晨
        current_activity: Optional[str] = None
        current_description: Optional[str] = None
        current_activity_type: Optional[str] = None
        current_goal_created_at: Any = None

        for goal, tw, is_today, is_yesterday in scheduled_goals:
            start_minutes, end_minutes = parse_time_window(tw)
            if start_minutes is None:
                continue

            is_cross_day = end_minutes > 1440

            if is_today:
                if is_cross_day:
                    is_in_window = (start_minutes <= current_time_minutes < 1440) or (
                        0 <= current_time_minutes < (end_minutes - 1440)
                    )
                else:
                    is_in_window = start_minutes <= current_time_minutes < end_minutes
            elif is_yesterday and is_cross_day:
                # 昨日的跨夜活动今天凌晨延续部分
                is_in_window = 0 <= current_time_minutes < (end_minutes - 1440)
            else:
                continue

            if is_in_window:
                # 多个匹配时，选创建时间最新（防 tz-aware/naive 混比报错）
                if current_activity is None or (
                    goal.created_at and (
                        current_goal_created_at is None
                        or strip_tz(goal.created_at) > strip_tz(current_goal_created_at)
                    )
                ):
                    current_activity = goal.name
                    current_description = goal.description
                    current_activity_type = goal.goal_type
                    current_goal_created_at = goal.created_at

        # 收集所有未来活动（仅今天创建的活动）
        all_future_activities: List[Tuple[str, str]] = []
        for goal, tw, is_today, _is_yesterday in scheduled_goals:
            if not is_today:
                continue
            start_val = tw[0] if len(tw) > 0 else 0
            end_val = tw[1] if len(tw) > 1 else start_val + 60
            start_minutes = start_val if end_val > 24 else start_val * 60

            if start_minutes > current_time_minutes:
                hour = start_minutes // 60
                minute = start_minutes % 60
                time_str = f"{hour:02d}:{minute:02d}"
                all_future_activities.append((time_str, goal.name))

        result = (current_activity, current_description, all_future_activities, current_activity_type)
        await self._schedule_cache.set(cache_key, result)
        return result
