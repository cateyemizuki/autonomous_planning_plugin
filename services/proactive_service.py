"""活动驱动的主动行为服务（v4.4 新增）。

两个职责合并在一个后台循环里：

1. **活动切换瞬间主动开口** —— 通过 ``ctx.maisaka.trigger_proactive`` 触发
   Maisaka 跑一轮主动对话；让 bot 在 12:00 切到午餐时自然在群里说一句
   "我去吃饭啦"。

2. **按 goal_type / priority 调节聊天频率** —— 通过
   ``ctx.frequency.set_adjust`` 把频率因子推给 heartflow，让 bot 在学习/工作时
   少说话、休息/娱乐时多说话。

两个能力都需要主程序提供（SDK v2.4+），如果 ctx 不可用会优雅降级。

设计要点：
    - 单独的 ``proactive_streams`` 白名单（默认空 = 完全禁用），避免误打扰
    - 每个 ``(stream_id, activity_name, date)`` 组合只主动触发一次
    - 频率因子按 goal_type 静态表映射，更新时记录避免重复 set_adjust
    - 后台循环 60 秒一次，启动延迟 15 秒等其他组件就绪
"""

from typing import TYPE_CHECKING, Dict, Optional, Tuple
import asyncio
import logging
import time

from ..planner.goal_manager import get_goal_manager
from ..utils.time_utils import parse_time_window
from ..utils.timezone_manager import TimezoneManager

if TYPE_CHECKING:
    from ..plugin import AutonomousPlanningPluginV4


logger = logging.getLogger(__name__)


# goal_type → 频率因子映射（1.0 = 主程序默认；<1 减少回复；>1 增加回复）
_FREQUENCY_FACTOR_BY_GOAL_TYPE: Dict[str, float] = {
    "study": 0.3,           # 学习：低频，不打扰
    "work": 0.3,            # 工作：低频
    "learn_topic": 0.5,     # 学习兴趣话题：偏低
    "sleep": 0.05,          # 睡觉：几乎不说话
    "exercise": 0.7,        # 运动：偏低
    "meal": 1.2,            # 吃饭：略高于平时
    "rest": 1.3,            # 休息：高频
    "daily_routine": 1.0,   # 日常作息：默认
    "entertainment": 1.5,   # 娱乐：高频
    "social_maintenance": 1.5,  # 社交：最高
}


# 主动发起的 intent 模板：按 goal_type 提供给 maisaka 当作主动开口的提示
_PROACTIVE_INTENT_TEMPLATES: Dict[str, str] = {
    "study": "Bot 刚开始学习/写代码，可以自然地在群里说一两句开始投入工作的话",
    "work": "Bot 刚开始工作，可以自然地说一两句进入状态的话",
    "meal": "Bot 刚开始吃饭，可以自然地在群里说一两句关于这顿饭的话",
    "rest": "Bot 刚开始休息，可以自然地说一两句放松/闲下来的话",
    "exercise": "Bot 刚开始运动，可以自然地在群里吼一两句锻炼的话",
    "entertainment": "Bot 刚开始娱乐/看剧/打游戏，可以自然地说一两句兴奋/期待的话",
    "social_maintenance": "Bot 现在心情比较social，可以主动找群友聊聊天",
    "daily_routine": "Bot 刚切换到日常活动，可以自然地说一句近况",
    "learn_topic": "Bot 开始钻研感兴趣的话题，可以自然地分享一点新发现",
}
_DEFAULT_PROACTIVE_INTENT = "Bot 切换到新活动了，可以自然地在群里说一两句近况"


class ProactiveService:
    """活动驱动的主动行为服务（v4.4 新增）。"""

    # 活动开始后 ≤ 此分钟内才考虑触发主动发起（避免半天后突然冒一句）
    PROACTIVE_FRESH_WINDOW_MINUTES = 5
    # 后台循环周期（秒）
    LOOP_INTERVAL_SECONDS = 60.0
    # 启动延迟（秒），等其他组件就绪
    STARTUP_DELAY_SECONDS = 15.0

    def __init__(self, plugin: "AutonomousPlanningPluginV4") -> None:
        """初始化 ProactiveService。

        Args:
            plugin: 当前插件实例
        """
        self._plugin = plugin
        self._stop_event: asyncio.Event = asyncio.Event()
        # 时区管理器（与 InjectService 用同一个时区）
        self._tz_manager: TimezoneManager = TimezoneManager(plugin.config.schedule.timezone)
        # 已触发的主动发起记录：{(stream_id, activity_name, date_str): trigger_time}
        # 同一个活动在同一天同一个 stream 只主动触发一次
        self._proactive_history: Dict[Tuple[str, str, str], float] = {}
        # 已应用的频率因子缓存：{stream_id: factor}（避免重复 set_adjust）
        self._current_factor: Dict[str, float] = {}
        logger.debug("ProactiveService 初始化（v4.4）")

    async def run_loop(self) -> None:
        """后台循环：60 秒检查一次活动状态，触发主动发起 + 频率调控。"""
        logger.info("🌟 活动驱动主动行为循环已启动（v4.4）")
        try:
            # 启动延迟：等待 InjectService 缓存预热 / chat_manager 就绪
            await asyncio.sleep(self.STARTUP_DELAY_SECONDS)
            while not self._stop_event.is_set():
                try:
                    await self._check_and_act()
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"主动检查异常: {exc}", exc_info=True)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.LOOP_INTERVAL_SECONDS,
                    )
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            logger.debug("主动行为循环被取消")
            raise
        finally:
            logger.info("🛑 主动行为循环已退出")

    async def stop(self) -> None:
        """通知循环停止。"""
        self._stop_event.set()

    # ------------------------------------------------------------
    # 主循环逻辑
    # ------------------------------------------------------------

    async def _check_and_act(self) -> None:
        """读当前活动 → 决定是否触发主动发起 / 调整频率。"""
        cfg = self._plugin.config.schedule
        proactive_streams = cfg.proactive_streams
        if not proactive_streams:
            # 白名单为空 = 主动行为完全禁用（默认安全）
            return

        # 找当前正在进行的活动
        activity_info = await self._find_current_activity()
        if activity_info is None:
            return
        activity_name, goal_type, start_minutes, _end_minutes = activity_info

        now = self._tz_manager.get_now()
        current_min = now.hour * 60 + now.minute
        date_str = now.strftime("%Y-%m-%d")

        # 1. 频率调控（每个 stream 独立）
        if cfg.enable_frequency_modulation:
            await self._apply_frequency(proactive_streams, goal_type)

        # 2. 主动发起判定（只在活动刚切换的前 5 分钟内才尝试）
        if not cfg.enable_proactive_trigger:
            return
        if (current_min - start_minutes) > self.PROACTIVE_FRESH_WINDOW_MINUTES:
            return  # 错过新鲜窗口

        for stream_id in proactive_streams:
            key = (stream_id, activity_name, date_str)
            if key in self._proactive_history:
                continue  # 同一天同一活动只触发一次
            await self._trigger_proactive(stream_id, activity_name, goal_type)
            self._proactive_history[key] = time.time()

    async def _find_current_activity(
        self,
    ) -> Optional[Tuple[str, str, int, int]]:
        """从 GoalManager 找当前正在进行的活动。

        Returns:
            ``(name, goal_type, start_minutes, end_minutes)``，没找到时为 ``None``。
        """
        try:
            gm = get_goal_manager()
            goals = gm.get_active_goals(chat_id="global")
            if not goals:
                return None
            now = self._tz_manager.get_now()
            current_min = now.hour * 60 + now.minute

            for goal in goals:
                # 跳过 pending_commitment 等非日程活动
                if goal.goal_type == "pending_commitment":
                    continue
                tw = None
                if goal.parameters and "time_window" in goal.parameters:
                    tw = goal.parameters.get("time_window")
                elif goal.conditions:
                    tw = goal.conditions.get("time_window")
                if not tw:
                    continue
                start, end = parse_time_window(tw)
                if start is None or end is None:
                    continue
                # 跨夜归一化
                if end > 1440 and current_min < 720:
                    end -= 1440
                if start <= current_min < end:
                    return goal.name, goal.goal_type or "daily_routine", start, end
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"查找当前活动失败: {exc}")
        return None

    # ------------------------------------------------------------
    # 频率调控
    # ------------------------------------------------------------

    async def _apply_frequency(self, streams: list[str], goal_type: str) -> None:
        """按 goal_type 映射频率因子并对每个 stream 应用。

        Args:
            streams: 目标聊天流 ID 列表
            goal_type: 当前活动类型
        """
        target_factor = _FREQUENCY_FACTOR_BY_GOAL_TYPE.get(goal_type, 1.0)
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "frequency"):
            return  # SDK 未提供（v2.4 以下版本兼容）

        for stream_id in streams:
            # 同一 stream 因子不变则跳过，避免反复 set_adjust 刷日志
            if self._current_factor.get(stream_id) == target_factor:
                continue
            try:
                result = await ctx_obj.frequency.set_adjust(stream_id, target_factor)
                # SDK 返回 {"success": bool, ...}
                if isinstance(result, dict) and not result.get("success", False):
                    logger.debug(
                        f"频率调控失败: stream={stream_id}, factor={target_factor}, "
                        f"reason={result.get('error')}"
                    )
                    continue
                self._current_factor[stream_id] = target_factor
                logger.info(
                    f"🎚️ 频率调控: stream={stream_id}, "
                    f"goal_type={goal_type}, factor={target_factor}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"频率调控异常: stream={stream_id}, {exc}")

    # ------------------------------------------------------------
    # 主动发起
    # ------------------------------------------------------------

    async def _trigger_proactive(
        self,
        stream_id: str,
        activity_name: str,
        goal_type: str,
    ) -> None:
        """对单个聊天流触发一次主动开口。

        Args:
            stream_id: 目标聊天流 ID
            activity_name: 当前活动名（用于注入 intent prompt）
            goal_type: 当前活动类型（决定 intent 模板）
        """
        ctx_obj = getattr(self._plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "maisaka"):
            return

        template = _PROACTIVE_INTENT_TEMPLATES.get(goal_type, _DEFAULT_PROACTIVE_INTENT)
        intent_prompt = f"{template}（活动名：{activity_name}）"

        try:
            result = await ctx_obj.maisaka.trigger_proactive(
                stream_id=stream_id,
                intent=intent_prompt,
                reason=f"autonomous_planning_v4: 活动切换 → {activity_name}",
                priority="normal",
            )
            if isinstance(result, dict) and not result.get("success", False):
                logger.warning(
                    f"主动发起被拒: stream={stream_id}, activity={activity_name}, "
                    f"reason={result.get('error')}"
                )
                return
            logger.info(
                f"🚀 主动发起触发: stream={stream_id}, activity={activity_name}, "
                f"goal_type={goal_type}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"主动发起异常: stream={stream_id}, {exc}")
