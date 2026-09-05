"""Automatic Schedule Scheduler.

This module provides automatic scheduling functionality that generates
daily schedules at configured times.

The scheduler runs as a background task and automatically creates
new daily schedules based on configuration settings.

Example:
    >>> from auto_scheduler import ScheduleAutoScheduler
    >>> scheduler = ScheduleAutoScheduler(plugin)
    >>> await scheduler.start()  # Start background task
"""

from typing import Optional
import asyncio
import datetime
import logging

from ..utils.timezone_manager import TimezoneManager
from .generator import BaseScheduleGenerator

logger = logging.getLogger(__name__)


class ScheduleAutoScheduler:
    """
    日程自动调度器类

    负责管理日程的定时生成任务，在每天配置的时间自动生成新一天的日程。

    Attributes:
        plugin: 插件实例引用
        is_running (bool): 任务运行状态
        task: 异步任务对象
        logger: 日志记录器
        _retry_count (int): P1优化 - 连续失败计数
        _max_retry_wait (int): P1优化 - 最大重试等待时间（秒）

    Methods:
        start: 启动定时任务
        stop: 停止定时任务
        _schedule_loop: 定时任务循环
        _generate_today_schedule: 生成今日日程
    """

    def __init__(self, plugin):
        """
        初始化定时任务调度器

        Args:
            plugin: 插件实例，用于获取配置和执行日程生成
        """
        self.plugin = plugin
        self.is_running = False
        self.task = None
        self.logger = logging.getLogger(__name__)

        # 初始化时区管理器
        timezone_str = plugin.config.schedule.timezone
        self.tz_manager = TimezoneManager(timezone_str)

        # P1优化：指数退避参数
        self._retry_count = 0
        self._max_retry_wait = 300  # 最大等待5分钟
        self._last_schedule_date: Optional[str] = None
        # 导入依赖（延迟导入避免循环依赖）
        from .goal_manager import get_goal_manager
        from .schedule_generator import ScheduleGenerator

        self.get_goal_manager = get_goal_manager
        self.ScheduleGenerator = ScheduleGenerator

    async def start(self):
        """
        启动定时任务

        检查插件配置，如果启用了定时生成功能，则启动定时任务。
        启动成功后会创建异步任务循环，等待配置的时间点执行日程生成。
        """
        if self.is_running:
            return

        # 检查是否启用定时生成
        enabled = self.plugin.config.schedule.auto_schedule_enabled
        if not enabled:
            self.logger.info("日程定时生成功能未启用")
            return

        self.is_running = True
        self.task = asyncio.create_task(self._schedule_loop())
        schedule_time = self.plugin.config.schedule.auto_schedule_time
        self.logger.info(f"日程定时生成已启动 - 执行时间: {schedule_time}")

    async def stop(self):
        """
        停止定时任务

        取消正在运行的定时任务，并等待任务完全结束。
        确保资源正确释放，避免任务泄漏。
        """
        if not self.is_running:
            return

        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.logger.info("日程定时任务已停止")

    @staticmethod
    def _normalize_time_str(time_str: str, default: str) -> str:
        try:
            hour_str, minute_str = str(time_str).split(":", 1)
            hour = int(hour_str)
            minute = int(minute_str)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        except Exception:
            pass
        return default

    def _is_time_due(self, now: datetime.datetime, time_str: str, last_date: Optional[str]) -> bool:
        today_str = now.strftime("%Y-%m-%d")
        if last_date == today_str:
            return False
        normalized = self._normalize_time_str(time_str, "00:30")
        hour_str, minute_str = normalized.split(":")
        target_time = now.replace(hour=int(hour_str), minute=int(minute_str), second=0, microsecond=0)
        return now >= target_time

    def _seconds_until_next(self, now: datetime.datetime, time_str: str, last_date: Optional[str]) -> float:
        normalized = self._normalize_time_str(time_str, "00:30")
        hour_str, minute_str = normalized.split(":")
        target_time = now.replace(hour=int(hour_str), minute=int(minute_str), second=0, microsecond=0)
        today_str = now.strftime("%Y-%m-%d")

        if last_date != today_str and now < target_time:
            return max(1.0, (target_time - now).total_seconds())

        next_target = target_time + datetime.timedelta(days=1)
        return max(1.0, (next_target - now).total_seconds())

    async def _schedule_loop(self):
        """
        定时任务循环

        持续运行的异步循环，计算下次执行时间并等待。
        当到达配置的时间点时，自动执行日程生成任务。

        循环会处理异常情况，确保单次失败不会影响后续执行。
        """
        while self.is_running:
            try:
                now = self.tz_manager.get_now()
                schedule_time_str = self.plugin.config.schedule.auto_schedule_time
                today_str = now.strftime("%Y-%m-%d")

                if self._is_time_due(now, schedule_time_str, self._last_schedule_date):
                    await self._generate_today_schedule()
                    self._last_schedule_date = today_str
                    self._retry_count = 0
                    continue

                wait_seconds = max(
                    15.0,
                    self._seconds_until_next(now, schedule_time_str, self._last_schedule_date),
                )
                await asyncio.sleep(wait_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"定时任务出错: {e}", exc_info=True)
                # P1优化：指数退避重试（30s, 60s, 120s, 240s, 300s）
                self._retry_count += 1
                wait_time = min(30 * (2 ** (self._retry_count - 1)), self._max_retry_wait)
                self.logger.info(f"将在 {wait_time} 秒后重试（第 {self._retry_count} 次）")
                await asyncio.sleep(wait_time)

    async def _generate_today_schedule(self):
        """
        生成今日日程

        定时任务的核心执行方法，自动生成今天的日程。
        完全静默运行，不发送任何消息到聊天，只记录日志。

        生成成功后会自动保存为目标，供后续使用。
        """
        try:
            # ✅ 使用时区管理器获取今天日期
            today = self.tz_manager.get_now().strftime("%Y-%m-%d")
            self.logger.info(f"🔄 开始自动生成今日日程: {today}")

            # 检查今天是否已有日程（修复：支持datetime对象）
            goal_manager = self.get_goal_manager()
            goals = goal_manager.get_all_goals(chat_id="global")

            today_has_schedule = False
            today_schedule_count = 0

            for goal in goals:
                # 检查目标是否有time_window（日程类型）
                time_window = None
                if goal.parameters and "time_window" in goal.parameters:
                    time_window = goal.parameters["time_window"]
                elif goal.conditions and "time_window" in goal.conditions:
                    time_window = goal.conditions["time_window"]

                # 如果有time_window且创建时间是今天，说明已有日程
                if time_window:
                    created_at = goal.created_at
                    goal_date = None

                    # 支持字符串和datetime对象
                    if isinstance(created_at, str):
                        goal_date = created_at.split("T")[0] if "T" in created_at else created_at[:10]
                    elif created_at:
                        goal_date = created_at.strftime("%Y-%m-%d")

                    if goal_date == today:
                        today_has_schedule = True
                        today_schedule_count += 1

            if today_has_schedule:
                self.logger.info(f"📅 今日已有 {today_schedule_count} 个日程，跳过自动生成")
                return

            # 生成日程
            # 使用 plugin.build_schedule_config 统一构建（含 bot_profile），
            # 不用 schedule.model_dump()（会漏掉 bot_profile 字段，导致 prompt
            # 中"未配置或为空"报错）
            schedule_config = self.plugin.build_schedule_config()

            schedule_generator = self.ScheduleGenerator(
                goal_manager=goal_manager,
                config=schedule_config,
                plugin=self.plugin,  # v4: 注入 plugin 引用以便用 ctx.llm.generate
            )

            schedule = await schedule_generator.generate_daily_schedule(
                user_id="system",
                chat_id="global",
                preferences={},
                use_llm=True,
                use_multi_round=self.plugin.config.schedule.use_multi_round,
            )

            # 🔧 修复：如果日程已存在，跳过应用
            if schedule.metadata and schedule.metadata.get("existing"):
                self.logger.info(f"📅 今天已有日程（{len(schedule.items)}个活动），跳过应用")
                return

            # 应用日程
            created_ids = await schedule_generator.apply_schedule(
                schedule=schedule, user_id="system", chat_id="global", auto_start=True
            )

            if created_ids:
                self.logger.info(f"✅ 自动生成日程成功: {today} - 创建了 {len(created_ids)} 个活动")
            else:
                self.logger.warning(f"⚠️ 自动生成日程失败: {today} - 未创建任何活动")

        except Exception as e:
            self.logger.error(f"自动生成日程出错: {e}", exc_info=True)
