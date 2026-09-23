"""可注入时钟。

领域服务不直接读取系统时间：所有"现在"都来自 Clock，便于在测试中
推演历史日期、迟到问题与逾期提醒。真实部署使用 SystemClock，
测试与回放使用 FixedClock。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回当前时刻（带时区）。"""


class SystemClock:
    """读取系统 UTC 时间的时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """固定（或可手工推进）的时钟，供测试与进程恢复演练使用。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = _aware(moment)

    def now(self) -> datetime:
        return self._moment

    def advance(self, delta) -> None:
        self._moment = _aware(self._moment + delta)

    def set(self, moment: datetime) -> None:
        self._moment = _aware(moment)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """统一为 UTC 可比时间。"""
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc)
