"""只增事件存储。

事件一经接收，其标识、发生时间与载荷不可原地修改；任何更正都必须
产生后继事件。存储支持按聚合/事件类型读取，以及整体导出恢复，
从而保证进程重启后截止点等状态完全沿用原记录。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .clock import as_utc
from .model import ConcurrencyError


class EventStore:
    def __init__(self) -> None:
        self._events: list[dict] = []
        self._by_aggregate: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self._ids: set[str] = set()

    # ---- 写入 ----
    def append(
        self,
        *,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: datetime,
        payload: dict | None = None,
        expected_version: int | None = None,
        summary: str = "",
    ) -> dict:
        if not event_id:
            raise ValueError("event_id 不能为空")
        if event_id in self._ids:
            raise ValueError(f"事件标识重复：{event_id}")
        key = (aggregate_type, aggregate_id)
        current_version = len(self._by_aggregate[key])
        if expected_version is not None and expected_version != current_version:
            raise ConcurrencyError(
                f"聚合 {aggregate_id} 版本冲突：期望 {expected_version}，实际 {current_version}"
            )
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": as_utc(occurred_at).isoformat(),
            "version": current_version + 1,
            "summary": summary,
            "payload": dict(payload or {}),
        }
        self._events.append(event)
        self._by_aggregate[key].append(event)
        self._ids.add(event_id)
        return dict(event)

    # ---- 读取 ----
    def stream(self, aggregate_type: str | None = None, aggregate_id: str | None = None) -> list[dict]:
        if aggregate_id is None and aggregate_type is None:
            return [dict(e) for e in self._events]
        if aggregate_id is not None and aggregate_type is not None:
            return [dict(e) for e in self._by_aggregate.get((aggregate_type, aggregate_id), ())]
        # 只按类型过滤
        return [dict(e) for e in self._events if e["aggregate_type"] == aggregate_type]

    def events_of_type(self, *event_types: str) -> list[dict]:
        wanted = set(event_types)
        return [dict(e) for e in self._events if e["event_type"] in wanted]

    def get(self, event_id: str) -> dict | None:
        for event in self._events:
            if event["event_id"] == event_id:
                return dict(event)
        return None

    def version_of(self, aggregate_type: str, aggregate_id: str) -> int:
        return len(self._by_aggregate.get((aggregate_type, aggregate_id), ()))

    # ---- 持久化：整体导出/恢复（进程恢复后沿用原截止点） ----
    def to_json(self) -> str:
        return json.dumps(self._events, ensure_ascii=False, indent=2)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, source: str | Path | list[dict]) -> "EventStore":
        store = cls()
        if isinstance(source, list):
            records = source
        else:
            text = Path(source).read_text(encoding="utf-8")
            records = json.loads(text) if text.strip() else []
        # 按存储顺序重放，恢复索引与版本；不重新编号、不改写时间。
        for raw in records:
            event = dict(raw)
            event.setdefault("payload", {})
            key = (event["aggregate_type"], event["aggregate_id"])
            if event["event_id"] in store._ids:
                raise ValueError(f"恢复数据存在重复事件标识：{event['event_id']}")
            expected = len(store._by_aggregate[key]) + 1
            if event["version"] != expected:
                raise ValueError(
                    f"恢复数据版本不接续：{event['aggregate_id']} 第 {event['version']} 条，期望 {expected}"
                )
            store._events.append(event)
            store._by_aggregate[key].append(event)
            store._ids.add(event["event_id"])
        return store
