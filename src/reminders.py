"""升级与逾期提醒。

提醒本身不落业务事件：截止点保存在 FREEZE_IMPOSED 事件载荷里，
每次都由注入时钟相对于“原始截止点”重新计算。
因此进程重启后从事件存储恢复，沿用的仍是当初那条决定中的 deadline，
不会因停机时间而漂移。
"""
from __future__ import annotations

from .clock import Clock
from .event_store import EventStore
from .textbook_service import (
    Projection, ALL_LAYERS,
)


class ReminderService:
    def __init__(self, store: EventStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def overdue_freezes(self) -> list[dict]:
        """已过确认截止点仍未全部解除的冻结（逾期提醒）。"""
        proj = Projection(self.store)
        now = self.clock.now()
        out: list[dict] = []
        for notice in proj.notices.values():
            if notice["state"] != "frozen" or not notice.get("deadline"):
                continue
            if notice["deadline"] > now:
                continue  # 未到原截止点不产生逾期提醒
            units = notice.get("affected_units", [notice["unit_id"]])
            for adoption in proj.adoptions.values():
                if not any(iv.edition_id == notice["edition_id"]
                           for iv in adoption["intervals"]):
                    continue
                school_id = adoption["school_id"]
                progress = proj.confirm_progress(notice["notice_id"], school_id, units)
                pending = {u: [layer for layer in ALL_LAYERS if layer not in layers]
                           for u, layers in progress.items()
                           if set(layers) != set(ALL_LAYERS)}
                # 从未确认的学校也视为全部待办
                if not progress:
                    pending = {u: list(ALL_LAYERS) for u in units}
                if pending:
                    out.append({
                        "kind": "freeze_overdue",
                        "notice_id": notice["notice_id"],
                        "school_id": school_id,
                        "course": adoption["course"],
                        "deadline": notice["deadline"].isoformat(),
                        "overdue_seconds": max(0, int((now - notice["deadline"]).total_seconds())),
                        "pending_layers_by_unit": pending,
                    })
        return sorted(out, key=lambda r: (r["deadline"], r["school_id"]))

    def available_upgrades(self, school_id: str) -> list[dict]:
        """学校当前采用版次之后已发布、且收录了其待处理勘误的可用版次（升级提醒）。"""
        proj = Projection(self.store)
        now = self.clock.now()
        upgrades: list[dict] = []
        for adoption in proj.adoptions.values():
            if adoption["school_id"] != school_id:
                continue
            current = proj.edition_at(adoption, now)
            if current is None:
                continue
            current_ed = proj.editions[current.edition_id]
            for edition_id, edition in proj.editions.items():
                if edition_id == current.edition_id:
                    continue
                if edition.state != "released" or edition.course != current_ed.course \
                        or edition.stage != current_ed.stage:
                    continue
                if not edition.released_at or not current_ed.released_at:
                    continue
                if edition.released_at <= current_ed.released_at:
                    continue
                notices = list(edition.included_notices)
                if notices:
                    upgrades.append({
                        "kind": "edition_upgrade",
                        "school_id": school_id, "course": adoption["course"],
                        "current_edition_id": current.edition_id,
                        "available_edition_id": edition_id,
                        "included_notice_ids": notices,
                        "released_at": edition.released_at.isoformat(),
                    })
        return upgrades

    def due(self, school_id: str | None = None) -> list[dict]:
        """汇总提醒；逾期优先于升级。"""
        overdue = self.overdue_freezes()
        if school_id is not None:
            overdue = [r for r in overdue if r["school_id"] == school_id]
            upgrades = self.available_upgrades(school_id)
        else:
            upgrades = []
            for adoption in Projection(self.store).adoptions.values():
                upgrades.extend(self.available_upgrades(adoption["school_id"]))
        return overdue + upgrades
