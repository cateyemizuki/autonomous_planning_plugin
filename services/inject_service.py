"""日程注入业务实现。

对应旧版 ``handlers/handlers.py:ScheduleInjectEventHandler``，但**注入入口已从
POST_LLM 事件改为 HookHandler("maisaka.planner.before_request")**（详见
POC_RESULT.md）。

业务子模块（IntentClassifier / ActivityStateAnalyzer / InjectOptimizer /
ContentTemplateEngine / ConversationContextCache）从 v3 直接复用。

核心改造：v3 修改 ``message.llm_prompt: str``（追加文本），v4 改为操作
``messages: list[PromptMessage]``（在第一个 system 消息后插入新的 system 消息）。
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
import asyncio
import logging
import re
import time
from datetime import timedelta

from ..cache.lru_cache import LRUCache
from ..handlers.inject import UserIntent
from ..planner.goal_manager import get_goal_manager
from ..utils.stream_filter import is_stream_allowed
from ..utils.time_utils import parse_time_window, strip_tz
from ..utils.timezone_manager import TimezoneManager

if TYPE_CHECKING:
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
        self._inject_mode: str = self._plugin.config.inject.inject_mode

        self._load_smart_components()

        logger.debug(
            "InjectService 初始化完成（mode=%s, cache_max=%d, cache_ttl=%d）",
            self._inject_mode,
            cfg.cache_max_size,
            cfg.cache_ttl,
        )

    def _load_smart_components(self) -> None:
        """加载智能注入子模块，加载失败则降级为 traditional 模式。"""
        inj = self._plugin.config.inject

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
                "✅ 智能注入组件已加载 (mode=%s, intent=%s, state=%s, optimizer=%s, context=%d/%ds)",
                self._inject_mode,
                inj.enable_intent_classification,
                inj.enable_state_analysis,
                inj.enable_inject_optimization,
                inj.context_max_turns,
                inj.context_ttl,
            )
        except ImportError as exc:
            logger.warning(f"智能注入组件加载失败，降级为 traditional 模式: {exc}")
            self._inject_mode = "traditional"
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
                self._get_current_schedule(chat_id)
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

    @staticmethod
    def _build_replyer_extra_prompt(
        current_activity: str,
        description: str,
        future_activities: List[Tuple[str, str]],
    ) -> str:
        """构建 replyer 用的轻量级 extra_prompt（突出活人感）。"""
        lines: List[str] = ["【角色当前状态】"]
        if description:
            lines.append(f"你现在正在 {current_activity}（{description}）。")
        else:
            lines.append(f"你现在正在 {current_activity}。")
        if future_activities:
            time_str, name = future_activities[0]
            lines.append(f"再过一会儿 {time_str} 要 {name}。")
        lines.extend([
            "",
            "⚠️ 这是你此刻的真实状态，**不要主动提及**。",
            "仅当用户明确询问 / 对话强相关时，才自然地用一两句话带过，不要刻意转移话题。",
        ])
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
        """从 PromptMessage 列表里提取最新一条 user 消息的文本内容。

        Args:
            messages: 序列化后的 PromptMessage dict 列表

        Returns:
            最后一条 user 消息的文本（多片段时取第一个文本片段；找不到时返回空串）
        """
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, str):
                        return part
                    if isinstance(part, dict) and "text" in part:
                        return str(part["text"])
                    if isinstance(part, (list, tuple)) and len(part) >= 2:
                        # (format, base64) 元组形式跳过
                        continue
            return ""
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
    # 注入文本构建（三种模式）
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
        """根据 inject_mode 构造注入文本。

        Returns:
            (inject_content, injected, detected_intent) 三元组
        """
        if self._inject_mode == "smart":
            return self._build_smart_text(
                user_message=user_message,
                current_activity=current_activity,
                current_description=current_description,
                future_activities=future_activities,
                activity_type=activity_type,
                context_continue_inject=context_continue_inject,
                context_reason=context_reason,
            )
        if self._inject_mode == "rule" and self._intent_classifier and self._inject_optimizer:
            return self._build_rule_text(
                user_message=user_message,
                current_activity=current_activity,
                current_description=current_description,
                future_activities=future_activities,
                context_continue_inject=context_continue_inject,
                context_reason=context_reason,
                user_id=user_id,
            )
        return self._build_traditional_text(
            current_activity=current_activity,
            current_description=current_description,
            future_activities=future_activities,
        )

    def _build_smart_text(
        self,
        user_message: str,
        current_activity: str,
        current_description: Optional[str],
        future_activities: List[Tuple[str, str]],
        activity_type: Optional[str],
        context_continue_inject: bool,
        context_reason: Optional[str],
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """LLM 软注入模式（推荐）：把日程作为可选上下文，由 LLM 自行判断使用。"""
        # 轻量级预判：技术问答 / 命令场景直接跳过
        msg_lower = user_message.lower() if user_message else ""
        is_command = user_message.startswith("/") or user_message.startswith("sudo") if user_message else False
        is_tech = any(kw in msg_lower for kw in ["怎么", "如何", "报错", "错误", "bug", "代码", "配置"])

        if is_command or is_tech:
            logger.debug("Smart 模式：检测到技术/命令场景，跳过注入")
            return None, False, "tech_or_command"

        if context_continue_inject:
            logger.info(f"📖 对话上下文触发注入: {context_reason}")

        inject_content = self._build_smart_inject_prompt(
            current_activity=current_activity,
            description=current_description or "",
            future_activities=future_activities,
            user_message=user_message,
            activity_type=activity_type,
        )
        logger.info(f"✅ Smart 注入: {current_activity}")
        return inject_content, True, None

    def _build_rule_text(
        self,
        user_message: str,
        current_activity: str,
        current_description: Optional[str],
        future_activities: List[Tuple[str, str]],
        context_continue_inject: bool,
        context_reason: Optional[str],
        user_id: str,
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """规则引擎模式：意图分类 + InjectOptimizer 判断 + ContentTemplateEngine 生成。"""
        if self._intent_classifier is None or self._inject_optimizer is None:
            return None, False, None

        intent, confidence = self._intent_classifier.classify(user_message)
        detected_intent = intent.value

        if context_continue_inject:
            logger.info(f"📖 对话上下文触发注入: {context_reason}")
            should_inject = True
            skip_reason: Optional[str] = None
        else:
            should_inject, skip_reason = self._inject_optimizer.should_inject(
                user_id, intent, current_activity, confidence,
            )

        if not should_inject:
            logger.debug(f"Rule 模式：InjectOptimizer 决定跳过注入: {skip_reason}")
            return None, False, detected_intent

        if not self._content_engine:
            return None, False, detected_intent

        enable_detailed_description = self._plugin.config.schedule.enable_detailed_description
        desc_to_inject = current_description if enable_detailed_description else None
        inject_content = self._content_engine.build_inject_content(
            intent=intent,
            current_activity=current_activity,
            current_description=desc_to_inject,
            activity_state=None,
            state_desc=desc_to_inject,
            next_activities=future_activities,
        )
        if self._inject_optimizer:
            self._inject_optimizer.record_injection(user_id, current_activity, inject_content or "", intent)

        logger.info(f"✅ Rule 注入: intent={detected_intent}, confidence={confidence:.2f}")
        return inject_content, True, detected_intent

    def _build_traditional_text(
        self,
        current_activity: str,
        current_description: Optional[str],
        future_activities: List[Tuple[str, str]],
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """传统模式：固定模板注入。"""
        enable_detailed_description = self._plugin.config.schedule.enable_detailed_description
        inject_content = f"【当前状态】\n这会儿正{current_activity}"
        if enable_detailed_description and current_description:
            inject_content += f"（{current_description}）"
        inject_content += "\n回复时可以自然提到当前在做什么，不要刻意强调。"
        if future_activities:
            next_time, next_activity = future_activities[0]
            inject_content += f"\n等下 {next_time} 要 {next_activity}。"
        inject_content += "\n"
        logger.info(f"✅ Traditional 注入: {current_activity}")
        return inject_content, True, None

    def _build_smart_inject_prompt(
        self,
        current_activity: str,
        description: str,
        future_activities: List[Tuple[str, str]],
        user_message: str,
        activity_type: Optional[str] = None,
    ) -> str:
        """构建 smart 模式的注入文本（LLM 软注入）。

        与 v3 实现等价。
        """
        del activity_type  # 当前未使用，保留参数兼容未来扩展

        msg_lower = (user_message or "").lower()
        is_direct_query = any(kw in msg_lower for kw in [
            "在干嘛", "做什么", "忙吗", "在做", "正在",
            "日程", "计划", "安排", "行程",
            "现在", "当前", "这会儿",
        ])
        is_future_query = any(kw in msg_lower for kw in [
            "接下来", "等下", "稍后", "之后", "待会",
            "明天", "今晚", "晚上", "下午", "上午",
        ])
        is_greeting = any(kw in msg_lower for kw in [
            "早上好", "晚上好", "早安", "晚安",
            "你好", "hi", "hello", "嗨",
        ])
        is_tech_question = any(kw in msg_lower for kw in [
            "怎么", "如何", "为什么", "什么是",
            "报错", "错误", "bug", "异常",
            "代码", "配置", "安装", "调试",
        ])
        is_command = user_message.startswith("/") or user_message.startswith("sudo") if user_message else False

        cfg = self._plugin.config.schedule
        enable_detailed_description = cfg.enable_detailed_description
        max_show = cfg.max_future_activities

        prompt_parts: List[str] = ["【可选上下文 - Bot 的当前日程】"]
        if enable_detailed_description and description:
            prompt_parts.append(f"现在：{current_activity}（{description}）")
        else:
            prompt_parts.append(f"现在：{current_activity}")

        if future_activities:
            prompt_parts.append("接下来的安排:")
            for time_str, activity_name in future_activities[:max_show]:
                prompt_parts.append(f"  {time_str} - {activity_name}")
        prompt_parts.append("")

        # 使用指导
        if is_command:
            prompt_parts.append("⚠️ 用户正在执行命令，请忽略以上日程信息，专注处理命令。")
        elif is_tech_question:
            prompt_parts.append("⚠️ 用户在询问技术问题，请忽略以上日程信息，专注回答技术内容。")
        elif is_direct_query:
            prompt_parts.append("💡 用户直接询问当前状态，请如实告知当前活动及状态。")
        elif is_future_query:
            prompt_parts.append("💡 用户询问未来计划，请自然地介绍后续安排。")
        elif is_greeting:
            prompt_parts.append("💡 用户在问候，可以自然地顺便提一下今天的计划（可选，不要强行提及）。")
        else:
            prompt_parts.append("💡 以上是 Bot 当前的日程信息，仅供参考。")
            prompt_parts.append("   - 如果与用户问题相关，可以自然提及")
            prompt_parts.append("   - 如果不相关，请完全忽略此信息")
            prompt_parts.append("   - 不要为了提及日程而刻意转移话题")

        prompt_parts.extend(["", "---", ""])
        return "\n".join(prompt_parts)

    # ------------------------------------------------------------
    # 对外公开：当前活动快照（供 @API 转发，跨插件可调）
    # ------------------------------------------------------------

    def get_current_activity_snapshot(self, chat_id: str = "global") -> Dict[str, Any]:
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
            self._get_current_schedule(chat_id or "global")
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
        """获取当前日程信息（带 15 分钟窗口缓存）。

        TTL 由 LRUCache 自身管理（构造时已配置 ``cfg.cache_ttl``），不再
        二次封装时间戳。缓存键按「chat_id + 日期 + 15 分钟窗口」分桶，
        同一窗口内反复查询直接命中。

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
