"""可注入时钟。

所有“现在”“截止时间”“提醒时刻”的判断都必须经过时钟，
禁止在业务代码中直接调用 ``datetime.now``，以便测试与进程恢复语义。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回当前时刻（带时区）。"""


class SystemClock:
    """生产时钟：读取系统 UTC 时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """固定时钟：测试用，可手动推进。"""

    def __init__(self, start: datetime) -> None:
        self._at = _ensure_aware(start)

    def now(self) -> datetime:
        return self._at

    def advance(self, **delta) -> "FixedClock":
        self._at = self._at + timedelta(**delta)
        return self

    def set(self, at: datetime) -> None:
        self._at = _ensure_aware(at)


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse(value: str | datetime) -> datetime:
    """把 ISO-8601 字符串解析为带时区的时间。"""
    if isinstance(value, datetime):
        return _ensure_aware(value)
    return _ensure_aware(datetime.fromisoformat(value))
