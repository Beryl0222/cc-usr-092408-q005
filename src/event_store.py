"""只增事件存储与事件信封。

核心约束：
- 事件一经接收，``event_id`` / ``occurred_at`` / ``version`` 不可原地改写；
  业务更正只能追加后继事件。
- 允许迟到事件按其 *实际发生时间* 入链（追加顺序与发生顺序解耦），
  投影一律按 ``occurred_at`` 排序回放。
- 每个聚合的 ``version`` 必须从 1 起严格连续，防止并发覆盖。
- 存储可落盘为 JSON，进程恢复后原样加载，截止点等状态随事件重建。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from .clock import parse


class EventError(ValueError):
    """事件追加违反只增语义。"""


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: dict = field(default_factory=dict)
    causation_id: str | None = None  # 触发本事件的上游事件 id（如勘误→冻结）

    def to_dict(self) -> dict:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            aggregate_type=data["aggregate_type"],
            aggregate_id=data["aggregate_id"],
            occurred_at=parse(data["occurred_at"]),
            version=int(data["version"]),
            summary=data["summary"],
            payload=data.get("payload", {}),
            causation_id=data.get("causation_id"),
        )


class EventStore:
    """内存只增日志，可选 JSON 文件持久化。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._events: list[Event] = []
        self._ids: set[str] = set()
        self._versions: dict[str, int] = {}
        if self._path and self._path.exists():
            self._load()

    # ---- 写入 ----------------------------------------------------------
    def append(
        self,
        event: Event,
        *,
        expected_version: int | None = None,
    ) -> Event:
        if event.event_id in self._ids:
            raise EventError(f"事件 id 已存在：{event.event_id}")
        if not isinstance(event.version, int) or event.version < 1:
            raise EventError("version 必须是正整数")
        last = self._versions.get(event.aggregate_id, 0)
        if expected_version is not None and expected_version != last:
            raise EventError(
                f"聚合 {event.aggregate_id} 版本冲突：期望 {expected_version}，实际 {last}"
            )
        if event.version != last + 1:
            raise EventError(
                f"聚合 {event.aggregate_id} 版本必须连续："
                f"收到 {event.version}，应为 {last + 1}"
            )
        self._events.append(event)
        self._ids.add(event.event_id)
        self._versions[event.aggregate_id] = event.version
        if self._path:
            self._persist()
        return event

    # ---- 读取 ----------------------------------------------------------
    def stream(self, aggregate_id: str) -> list[Event]:
        return [e for e in self._events if e.aggregate_id == aggregate_id]

    def by_type(self, event_type: str) -> list[Event]:
        return [e for e in self._events if e.event_type == event_type]

    def all(self) -> list[Event]:
        return list(self._events)

    def chronological(self) -> list[Event]:
        """按实际发生时间排序的全量视图（迟到事件在此归位）。"""
        return sorted(self._events, key=lambda e: (e.occurred_at, e.event_id))

    def version_of(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0)

    def __len__(self) -> int:
        return len(self._events)

    # ---- 持久化 --------------------------------------------------------
    def _persist(self) -> None:
        assert self._path is not None
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps([e.to_dict() for e in self._events], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def _load(self) -> None:
        assert self._path is not None
        records = json.loads(self._path.read_text(encoding="utf-8"))
        for raw in records:
            event = Event.from_dict(raw)
            # 直接装载，不再走版本校验之外的副作用路径
            if event.event_id in self._ids:
                raise EventError(f"持久化数据出现重复事件 id：{event.event_id}")
            last = self._versions.get(event.aggregate_id, 0)
            if event.version != last + 1:
                raise EventError(
                    f"持久化数据中聚合 {event.aggregate_id} 版本不连续："
                    f"{event.version} != {last + 1}"
                )
            self._events.append(event)
            self._ids.add(event.event_id)
            self._versions[event.aggregate_id] = event.version
