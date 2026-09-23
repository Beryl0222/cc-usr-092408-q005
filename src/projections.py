"""只读投影：重放事件流，回答"某一历史日期的文本是什么"。

投影不保存任何独立事实——全部状态由事件重放得到；进程恢复时
重新加载事件即可。所有区间均为左闭右开 [from, to)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .clock import as_utc
from .model import (
    ACK_CONFIRMED,
    EV_ADOPTION_FROZEN,
    EV_ADOPTION_RECORDED,
    EV_CORRECTION_APPROVED,
    EV_CONTENT_REFERENCE,
    EV_EDITION_RELEASED,
    EV_ISSUE_REPORTED,
    EV_LEGAL_BASIS_REGISTERED,
    EV_NOTICE_ACKNOWLEDGED,
    EV_REMINDER_DELIVERED,
    EV_REMINDER_SCHEDULED,
    EV_SWITCH_COMPLETED,
    EV_SWITCH_REQUESTED,
    EV_TEACHING_FACT,
    EV_UNIT_REGISTERED,
    REMINDER_DUE,
    REMINDER_ESCALATED,
    REMINDER_OVERDUE,
    ROUTE_FREEZE,
    STATUS_AVAILABLE,
    STATUS_FROZEN,
    SWITCH_LAYERS,
)


@dataclass
class _Binding:
    edition_id: str
    channel: str
    content_ref: str
    content_hash: str
    course: str | None
    stage: str | None
    from_at: datetime
    source_correction_id: str | None = None


@dataclass
class State:
    editions: dict = field(default_factory=dict)            # edition_id -> 元数据
    edition_books: dict = field(default_factory=lambda: {})  # book_id -> [edition_id] 时间序
    units: dict = field(default_factory=lambda: {})         # unit_id -> {code,title,bindings:[_Binding]}
    legal: dict = field(default_factory=dict)
    adoptions: dict = field(default_factory=dict)
    schools: dict = field(default_factory=lambda: {})       # school_id -> [adoption_id]
    issues: dict = field(default_factory=dict)
    corrections: dict = field(default_factory=dict)
    freezes: list = field(default_factory=list)             # ADOPTION_FROZEN 载荷
    acks: list = field(default_factory=list)
    switch_requests: list = field(default_factory=list)
    teaching_facts: list = field(default_factory=list)
    reminders: dict = field(default_factory=dict)
    events: list = field(default_factory=list)              # 保序的全部事件

    # ---- 基础引用 ----
    def edition(self, edition_id: str):
        return self.editions.get(edition_id)

    def unit(self, unit_id: str):
        return self.units.get(unit_id)

    def adoption_for(self, school_id: str, course: str, stage: str) -> str | None:
        for adoption_id in self.schools.get(school_id, ()):  # 较早登记在前
            adoption = self.adoptions[adoption_id]
            if adoption["course"] == course and adoption["stage"] == stage:
                return adoption_id
        return None

    def editions_of_book(self, book_id: str) -> list[str]:
        return list(self.edition_books.get(book_id, ()))

    # ---- 版次/采用的有效区间 ----
    def edition_at(self, edition_id: str, moment: datetime) -> bool:
        meta = self.editions.get(edition_id)
        return meta is not None and meta["released_at"] <= moment

    def adopted_edition(self, adoption_id: str, moment: datetime) -> str | None:
        adoption = self.adoptions.get(adoption_id)
        if adoption is None:
            return None
        current = None
        for step in adoption["editions"]:
            if step["from_at"] <= moment and (step["to_at"] is None or moment < step["to_at"]):
                current = step["edition_id"]
        return current

    def binding_at(self, unit_id: str, edition_id: str, channel: str, moment: datetime) -> _Binding | None:
        unit = self.units.get(unit_id)
        if unit is None:
            return None
        candidates = [
            b for b in unit["bindings"]
            if b.edition_id == edition_id and b.channel == channel and b.from_at <= moment
        ]
        return candidates[-1] if candidates else None

    # ---- 勘误确认进度（按时间点） ----
    def correction(self, correction_id: str):
        return self.corrections.get(correction_id)

    def confirmed_layers(self, correction_id: str, school_id: str, unit_id: str,
                         by_moment: datetime | None = None) -> set[str]:
        """截至某时刻已确认的层次；不传时刻表示当前态。"""
        layers = set()
        for ack in self.acks:
            if by_moment is not None and ack["at"] > by_moment:
                continue
            if (
                ack["correction_id"] == correction_id
                and ack["school_id"] == school_id
                and ack["result"] == ACK_CONFIRMED
                and unit_id in ack["unit_ids"]
            ):
                layers.add(ack["layer"])
        return layers

    def freeze_for(self, correction_id: str, school_id: str):
        for freeze in self.freezes:
            if freeze["correction_id"] == correction_id and freeze["school_id"] == school_id:
                return freeze
        return None

    def is_unit_released(self, correction_id: str, school_id: str, unit_id: str,
                         by_moment: datetime | None = None) -> bool:
        """三层全部确认才解除该单元；解除只对确认齐备之后的时刻成立。"""
        return self.confirmed_layers(
            correction_id, school_id, unit_id, by_moment
        ) >= set(SWITCH_LAYERS)

    def active_freeze(self, correction_id: str, school_id: str, unit_id: str, channel: str, moment: datetime) -> bool:
        correction = self.corrections.get(correction_id)
        freeze = self.freeze_for(correction_id, school_id)
        if correction is None or freeze is None:
            return False
        if freeze["frozen_at"] > moment:
            return False
        if unit_id not in correction["unit_ids"] or channel not in correction["channels"]:
            return False
        if unit_id not in freeze["unit_ids"] or channel not in freeze["channels"]:
            return False
        return not self.is_unit_released(correction_id, school_id, unit_id, moment)

    # ---- 历史文本还原 ----
    def text_at(self, school_id: str, unit_id: str, channel: str, moment: datetime) -> dict:
        moment = as_utc(moment)
        adoption_id = None
        for candidate in self.schools.get(school_id, ()):
            adoption = self.adoptions[candidate]
            if adoption["editions"][0]["from_at"] > moment:
                continue  # 该日采用关系尚未建立
            if unit_id in adoption.get("used_units", ()):
                adoption_id = candidate
                break
        if adoption_id is None:
            return {"status": "NO_ADOPTION", "at": moment.isoformat()}
        adoption = self.adoptions[adoption_id]
        edition_id = self.adopted_edition(adoption_id, moment)
        binding = self.binding_at(unit_id, edition_id, channel, moment) if edition_id else None
        if binding is None:
            return {
                "status": "NO_CONTENT",
                "school_id": school_id,
                "unit_id": unit_id,
                "channel": channel,
                "edition_id": edition_id,
                "at": moment.isoformat(),
            }
        result = {
            "status": STATUS_AVAILABLE,
            "school_id": school_id,
            "adoption_id": adoption_id,
            "unit_id": unit_id,
            "channel": channel,
            "edition_id": edition_id,
            "content_ref": binding.content_ref,
            "content_hash": binding.content_hash,
            "course": adoption["course"],
            "stage": adoption["stage"],
            "at": moment.isoformat(),
            "legal_basis_ids": [],
            "frozen_by": None,
        }
        # 当时生效的冻结优先：受影响内容不返回文本。
        for correction_id, correction in self.corrections.items():
            if correction["route"] == ROUTE_FREEZE and self.active_freeze(
                correction_id, school_id, unit_id, channel, moment
            ):
                result["status"] = STATUS_FROZEN
                result["frozen_by"] = correction_id
                result["legal_basis_ids"] = list(correction["legal_basis_ids"])
                result["content_ref"] = None
                result["content_hash"] = None
                return result
        # 标注该文本所依据的法规与勘误（不改变文本本身）。
        basis = []
        if binding.source_correction_id:
            correction = self.corrections.get(binding.source_correction_id)
            if correction:
                basis.extend(correction["legal_basis_ids"])
                result["correction_id"] = binding.source_correction_id
        result["legal_basis_ids"] = basis
        return result

    # ---- 监管追溯：从一条替换记录出发 ----
    def correction_trace(self, correction_id: str, moment: datetime | None = None) -> dict:
        moment = as_utc(moment) if moment else _last_event_time(self)
        correction = self.corrections.get(correction_id)
        if correction is None:
            return {"found": False, "correction_id": correction_id}
        # 实际生效的版次：由源于本勘误的内容绑定确定。
        effective_editions = set()
        for unit_id in correction["unit_ids"]:
            for binding in self.units.get(unit_id, {}).get("bindings", ()):
                if binding.source_correction_id == correction_id:
                    effective_editions.add(binding.edition_id)
        blocked = []
        for freeze in self.freezes:
            if freeze["correction_id"] != correction_id:
                continue
            school_id = freeze["school_id"]
            units_progress = []
            any_blocked = False
            for unit_id in freeze["unit_ids"]:
                layers = self.confirmed_layers(correction_id, school_id, unit_id)
                released = layers >= set(SWITCH_LAYERS)
                if not released:
                    any_blocked = True
                units_progress.append({
                    "unit_id": unit_id,
                    "confirmed_layers": sorted(layers),
                    "missing_layers": sorted(set(SWITCH_LAYERS) - layers),
                    "released": released,
                })
            adoption_id = freeze["adoption_id"]
            adoption = self.adoptions.get(adoption_id, {})
            current_edition = self.adopted_edition(adoption_id, moment) if adoption else None
            blocked.append({
                "school_id": school_id,
                "school_name": freeze.get("school_name"),
                "adoption_id": adoption_id,
                "course": adoption.get("course"),
                "stage": adoption.get("stage"),
                "current_edition_id": current_edition,
                "still_blocked": any_blocked,
                "units": units_progress,
            })
        return {
            "found": True,
            "correction_id": correction_id,
            "severity": correction["severity"],
            "route": correction["route"],
            "approved_at": correction["approved_at"].isoformat(),
            "issue_ids": list(correction["issue_ids"]),
            "affected_unit_ids": list(correction["unit_ids"]),
            "channels": list(correction["channels"]),
            "replacement": dict(correction["replacement"]),
            "legal_basis": [
                {"legal_basis_id": bid, **self.legal[bid]}
                for bid in correction["legal_basis_ids"]
                if bid in self.legal
            ],
            "effective_edition_ids": sorted(effective_editions),
            "schools": blocked,
            "blocked_school_ids": [b["school_id"] for b in blocked if b["still_blocked"]],
        }

    # ---- 提醒（升级/逾期） ----
    def due_reminders(self, moment: datetime) -> list[dict]:
        moment = as_utc(moment)
        due = []
        for reminder_id, reminder in self.reminders.items():
            sent_levels = set(reminder["sent_levels"])
            levels = []
            if moment == reminder["due_at"]:
                levels.append(REMINDER_DUE)
            elif moment > reminder["due_at"]:
                levels.append(REMINDER_OVERDUE)
            if reminder["escalate_at"] and moment >= reminder["escalate_at"]:
                levels.append(REMINDER_ESCALATED)
            for level in levels:
                if level in sent_levels:
                    continue
                due.append({"reminder_id": reminder_id, "level": level, **{
                    k: v for k, v in reminder.items() if k != "sent_levels"
                }})
        return due


def _last_event_time(state: State) -> datetime:
    if not state.events:
        from datetime import timezone
        return datetime.min.replace(tzinfo=timezone.utc)
    return state.events[-1]["_at"]


def replay(events: list[dict]) -> State:
    """按 (发生时间, 入链顺序) 重放；迟到问题以实际发现时间定位。"""
    state = State()
    ordered = sorted(enumerate(events), key=lambda pair: (_parse(pair[1]["occurred_at"]), pair[0]))
    for seq, event in ordered:
        apply_event(state, event, seq)
        state.events.append({**event, "_at": _parse(event["occurred_at"]), "_seq": seq})
    return state


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def apply_event(state: State, event: dict, seq: int) -> None:
    at = _parse(event["occurred_at"])
    p = event.get("payload", {})
    t = event["event_type"]

    if t == EV_EDITION_RELEASED:
        edition_id = event["aggregate_id"]
        state.editions[edition_id] = {
            "edition_id": edition_id,
            "book_id": p["book_id"],
            "edition_code": p["edition_code"],
            "title": p.get("title", ""),
            "released_at": at,
            "supersedes": p.get("supersedes"),
        }
        prior = state.edition_books.setdefault(p["book_id"], [])
        if edition_id not in prior:
            prior.append(edition_id)
        if p.get("supersedes") and p["supersedes"] in state.editions:
            state.editions[p["supersedes"]]["successor"] = edition_id

    elif t in (EV_UNIT_REGISTERED, EV_CONTENT_REFERENCE):
        unit_id = event["aggregate_id"]
        unit = state.units.setdefault(unit_id, {"unit_id": unit_id, "code": p.get("unit_code", unit_id),
                                                "title": p.get("title", ""), "bindings": []})
        unit["bindings"].append(_Binding(
            edition_id=p["edition_id"],
            channel=p["channel"],
            content_ref=p["content_ref"],
            content_hash=p["content_hash"],
            course=p.get("course"),
            stage=p.get("stage"),
            from_at=at,
            source_correction_id=p.get("source_correction_id"),
        ))

    elif t == EV_LEGAL_BASIS_REGISTERED:
        basis_id = event["aggregate_id"]
        state.legal[basis_id] = {
            "code": p["code"],
            "title": p.get("title", ""),
            "version": p["version"],
            "effective_from": p.get("effective_from"),
            "citation": p.get("citation", ""),
            "registered_at": at.isoformat(),
        }

    elif t == EV_ADOPTION_RECORDED:
        adoption_id = event["aggregate_id"]
        state.adoptions[adoption_id] = {
            "adoption_id": adoption_id,
            "school_id": p["school_id"],
            "school_name": p.get("school_name", ""),
            "course": p["course"],
            "stage": p["stage"],
            "local_supplement_id": p.get("local_supplement_id"),
            "local_supplement_hash": p.get("local_supplement_hash"),
            "used_units": list(p.get("used_unit_ids", ())),
            "editions": [{"edition_id": p["edition_id"], "from_at": at, "to_at": None}],
        }
        state.schools.setdefault(p["school_id"], []).append(adoption_id)

    elif t == EV_SWITCH_REQUESTED:
        state.switch_requests.append({"adoption_id": event["aggregate_id"], **p, "requested_at": at})

    elif t == EV_SWITCH_COMPLETED:
        adoption = state.adoptions[event["aggregate_id"]]
        adoption["editions"][-1]["to_at"] = at
        adoption["editions"].append({"edition_id": p["to_edition_id"], "from_at": at, "to_at": None})
        adoption["local_supplement_id"] = p.get("local_supplement_id", adoption["local_supplement_id"])
        adoption["local_supplement_hash"] = p.get("local_supplement_hash", adoption["local_supplement_hash"])

    elif t == EV_ISSUE_REPORTED:
        state.issues[event["aggregate_id"]] = {
            "issue_id": event["aggregate_id"],
            "discovered_at": _parse(p["discovered_at"]),
            "received_at": _parse(p["received_at"]) if p.get("received_at") else at,
            **{k: v for k, v in p.items() if k not in ("discovered_at", "received_at")},
        }

    elif t == EV_CORRECTION_APPROVED:
        correction_id = event["aggregate_id"]
        state.corrections[correction_id] = {
            "correction_id": correction_id,
            "severity": p["severity"],
            "route": p["route"],
            "issue_ids": list(p.get("issue_ids", ())),
            "unit_ids": list(p["unit_ids"]),
            "channels": list(p["channels"]),
            "replacement": dict(p.get("replacement", {})),
            "legal_basis_ids": list(p.get("legal_basis_ids", ())),
            "approved_at": at,
            "ack_deadline": _parse(p["ack_deadline"]) if p.get("ack_deadline") else None,
            "escalate_at": _parse(p["escalate_at"]) if p.get("escalate_at") else None,
        }

    elif t == EV_ADOPTION_FROZEN:
        p2 = dict(p)
        p2["correction_id"] = event["aggregate_id"]
        p2["frozen_at"] = at
        state.freezes.append(p2)

    elif t == EV_NOTICE_ACKNOWLEDGED:
        state.acks.append({
            "ack_event_id": event["event_id"],
            "correction_id": event["aggregate_id"],
            "school_id": p["school_id"],
            "layer": p["layer"],
            "unit_ids": list(p["unit_ids"]),
            "result": p["result"],
            "at": at,
        })

    elif t == EV_TEACHING_FACT:
        state.teaching_facts.append({
            "fact_id": event["aggregate_id"],
            "school_id": p["school_id"],
            "adoption_id": p.get("adoption_id"),
            "unit_id": p["unit_id"],
            "channel": p["channel"],
            "edition_id": p["edition_id"],
            "taught_at": _parse(p["taught_at"]),
            "content_ref": p.get("content_ref"),
            "content_hash": p["content_hash"],
        })

    elif t == EV_REMINDER_SCHEDULED:
        state.reminders[event["aggregate_id"]] = {
            "target_type": p["target_type"],
            "target_id": p["target_id"],
            "school_id": p.get("school_id"),
            "due_at": _parse(p["due_at"]),
            "escalate_at": _parse(p["escalate_at"]) if p.get("escalate_at") else None,
            "created_at": at,
            "sent_levels": [],
        }

    elif t == EV_REMINDER_DELIVERED:
        reminder = state.reminders.get(p["reminder_id"])
        if reminder is not None and p["level"] not in reminder["sent_levels"]:
            reminder["sent_levels"].append(p["level"])
