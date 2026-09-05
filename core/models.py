"""数据模型 - 自主规划插件

本模块定义了插件中使用的核心数据模型。

主要类：
    - ScheduleType: 日程类型枚举
    - ScheduleItem: 日程项
    - Schedule: 完整日程
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class ScheduleType(Enum):
    """日程类型枚举"""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class ScheduleItem:
    """日程项数据模型

    表示单个计划活动，包含名称、描述、时间等信息。
    """

    def __init__(
        self,
        name: str,
        description: str,
        goal_type: str,
        priority: str,
        time_slot: Optional[str] = None,
        duration_hours: Optional[float] = None,
        parameters: Optional[Dict[str, Any]] = None,
        conditions: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.description = description
        self.goal_type = goal_type
        self.priority = priority
        self.time_slot = time_slot
        self.duration_hours = duration_hours
        self.parameters = parameters or {}
        self.conditions = conditions or {}

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "name": self.name,
            "description": self.description,
            "goal_type": self.goal_type,
            "priority": self.priority,
            "time_slot": self.time_slot,
            "duration_hours": self.duration_hours,
            "parameters": self.parameters,
            "conditions": self.conditions,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduleItem":
        """从字典创建实例"""
        return cls(
            name=data["name"],
            description=data["description"],
            goal_type=data["goal_type"],
            priority=data["priority"],
            time_slot=data.get("time_slot"),
            duration_hours=data.get("duration_hours"),
            parameters=data.get("parameters"),
            conditions=data.get("conditions"),
        )

    def __repr__(self) -> str:
        return (
            f"ScheduleItem(name={self.name!r}, "
            f"time_slot={self.time_slot!r}, "
            f"duration={self.duration_hours}h)"
        )


class Schedule:
    """完整日程数据模型

    表示一个完整的日程计划，包含多个日程项。
    """

    def __init__(
        self,
        schedule_type: ScheduleType,
        name: str,
        items: List[ScheduleItem],
        created_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timezone: str = "Asia/Shanghai",
    ):
        self.schedule_type = schedule_type
        self.name = name
        self.items = items
        # 使用时区感知时间
        if created_at is None:
            from ..utils.timezone_manager import TimezoneManager
            tz_manager = TimezoneManager(timezone)
            created_at = tz_manager.get_now()
        self.created_at = created_at
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "schedule_type": self.schedule_type.value,
            "name": self.name,
            "items": [item.to_dict() for item in self.items],
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Schedule":
        """从字典创建实例"""
        return cls(
            schedule_type=ScheduleType(data["schedule_type"]),
            name=data["name"],
            items=[ScheduleItem.from_dict(item) for item in data["items"]],
            created_at=datetime.fromisoformat(data["created_at"]),
            metadata=data.get("metadata"),
        )

    def __repr__(self) -> str:
        return (
            f"Schedule(type={self.schedule_type.value}, "
            f"name={self.name!r}, "
            f"items={len(self.items)})"
        )

    def __len__(self) -> int:
        """返回日程项数量"""
        return len(self.items)
