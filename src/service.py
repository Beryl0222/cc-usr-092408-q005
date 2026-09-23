"""教材版次接续服务：命令侧。

所有写操作都转化为只增事件；"现在"由可注入时钟给出。
重大勘误冻结受影响内容并等待学校三层确认；普通勘误进入
下一个发布的可用版次；教学事实一经记录不可改写。
"""

from __future__ import annotations

from datetime import timedelta

from .clock import Clock, as_utc
from .model import (
    ACK_CONFIRMED,
    ACK_DUPLICATE,
    ACK_HELD_CONTENT_CHANGED,
    AGG_ADOPTION,
    AGG_CORRECTION,
    AGG_EDITION,
    AGG_ISSUE,
    AGG_LEGAL_BASIS,
    AGG_REMINDER,
    AGG_TEACHING_FACT,
    AGG_UNIT,
    CHANNELS,
    DomainError,
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
    ROUTE_FREEZE,
    ROUTE_NEXT_EDITION,
    SEVERITY_MAJOR,
    SEVERITY_NORMAL,
    SWITCH_LAYERS,
    UnknownReference,
)
from .projections import replay
from .store import EventStore

DEFAULT_ACK_WINDOW = timedelta(days=14)   # 学校确认期限
DEFAULT_ESCALATION = timedelta(days=7)    # 逾期后升级间隔


class TextbookLifecycleService:
    def __init__(self, store: EventStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    # ================= 读侧 =================
    def state(self):
        return replay(self.store.stream())

    def text_at(self, school_id: str, unit_id: str, channel: str, moment) -> dict:
        return self.state().text_at(school_id, unit_id, channel, moment)

    def trace_correction(self, correction_id: str, moment=None) -> dict:
        return self.state().correction_trace(correction_id, moment)

    # ================= 法规依据 =================
    def register_legal_basis(
        self,
        legal_basis_id: str,
        *,
        code: str,
        version: str,
        title: str = "",
        effective_from: str | None = None,
        citation: str = "",
    ) -> dict:
        return self._append(
            AGG_LEGAL_BASIS, legal_basis_id, EV_LEGAL_BASIS_REGISTERED,
            {
                "code": code, "version": version, "title": title,
                "effective_from": effective_from, "citation": citation,
            },
            summary=f"登记法规依据 {code}@{version}",
        )

    # ================= 版次与内容单元 =================
    def release_edition(
        self,
        edition_id: str,
        *,
        book_id: str,
        edition_code: str,
        title: str = "",
        supersedes: str | None = None,
    ) -> dict:
        state = self.state()
        if edition_id in state.editions:
            raise DomainError(f"版次已存在：{edition_id}")
        if supersedes is not None:
            prior = state.editions.get(supersedes)
            if prior is None:
                raise UnknownReference(f"被替代版次不存在：{supersedes}")
            if prior["book_id"] != book_id:
                raise DomainError("新旧版次必须属于同一教材")
        event = self._append(
            AGG_EDITION, edition_id, EV_EDITION_RELEASED,
            {"book_id": book_id, "edition_code": edition_code, "title": title,
             "supersedes": supersedes},
            summary=f"发布版次 {edition_code}",
        )
        # 已获批勘误的替换内容在此后发布的版次中自动接续；
        # 普通勘误由此首次生效，重大勘误的冻结修正同样成为新版次的既定文本。
        self._apply_pending_corrections(edition_id, book_id)
        return event

    def register_content(
        self,
        unit_id: str,
        *,
        edition_id: str,
        channel: str,
        content_ref: str,
        content_hash: str,
        unit_code: str | None = None,
        title: str = "",
        course: str | None = None,
        stage: str | None = None,
        source_correction_id: str | None = None,
    ) -> dict:
        if channel not in CHANNELS:
            raise DomainError(f"未知内容载体：{channel}")
        state = self.state()
        if edition_id not in state.editions:
            raise UnknownReference(f"版次不存在：{edition_id}")
        if source_correction_id and source_correction_id not in state.corrections:
            raise UnknownReference(f"勘误不存在：{source_correction_id}")
        event_type = EV_UNIT_REGISTERED if unit_id not in state.units else EV_CONTENT_REFERENCE
        return self._append(
            AGG_UNIT, unit_id, event_type,
            {
                "unit_code": unit_code or unit_id, "title": title,
                "edition_id": edition_id, "channel": channel,
                "content_ref": content_ref, "content_hash": content_hash,
                "course": course, "stage": stage,
                "source_correction_id": source_correction_id,
            },
            summary=f"登记 {edition_id}/{channel} 的单元内容 {unit_id}",
        )

    def _apply_pending_corrections(self, edition_id: str, book_id: str) -> None:
        state = self.state()
        for correction_id, correction in state.corrections.items():
            already_bound = any(
                b.source_correction_id == correction_id
                for unit_id in correction["unit_ids"]
                for b in state.units.get(unit_id, {}).get("bindings", ())
                if b.edition_id == edition_id
            )
            if already_bound:
                continue
            if not self._units_belong_to_book(state, correction["unit_ids"], book_id):
                continue
            for unit_id in correction["unit_ids"]:
                for channel in correction["channels"]:
                    replacement = correction["replacement"].get(channel)
                    if replacement is None:
                        continue
                    self.register_content(
                        unit_id,
                        edition_id=edition_id,
                        channel=channel,
                        content_ref=replacement["content_ref"],
                        content_hash=replacement["content_hash"],
                        source_correction_id=correction_id,
                    )
                    state = self.state()  # 后续绑定需要最新版本号

    @staticmethod
    def _units_belong_to_book(state, unit_ids: list[str], book_id: str) -> bool:
        for unit_id in unit_ids:
            unit = state.units.get(unit_id)
            if unit is None:
                return False
            editions = {b.edition_id for b in unit["bindings"]}
            if not any(state.editions.get(e, {}).get("book_id") == book_id for e in editions):
                return False
        return True

    # ================= 学校采用 =================
    def record_adoption(
        self,
        adoption_id: str,
        *,
        school_id: str,
        course: str,
        stage: str,
        edition_id: str,
        used_unit_ids: list[str],
        school_name: str = "",
        local_supplement_id: str | None = None,
        local_supplement_hash: str | None = None,
    ) -> dict:
        state = self.state()
        if adoption_id in state.adoptions:
            raise DomainError(f"采用关系已存在：{adoption_id}")
        if edition_id not in state.editions:
            raise UnknownReference(f"版次不存在：{edition_id}")
        event = self._append(
            AGG_ADOPTION, adoption_id, EV_ADOPTION_RECORDED,
            {
                "school_id": school_id, "school_name": school_name,
                "course": course, "stage": stage, "edition_id": edition_id,
                "used_unit_ids": list(used_unit_ids),
                "local_supplement_id": local_supplement_id,
                "local_supplement_hash": local_supplement_hash,
            },
            summary=f"{school_name or school_id} 采用 {edition_id}（{course}/{stage}）",
        )
        # 批准在先、采用在后的学校同样立即落入既有重大勘误冻结。
        self._freeze_new_adoption_for_existing_corrections(adoption_id)
        return event

    def _freeze_new_adoption_for_existing_corrections(self, adoption_id: str) -> None:
        state = self.state()
        adoption = state.adoptions[adoption_id]
        for correction_id, correction in state.corrections.items():
            if correction["route"] != ROUTE_FREEZE:
                continue
            if any(f["adoption_id"] == adoption_id for f in state.freezes
                   if f["correction_id"] == correction_id):
                continue
            if not any(u in correction["unit_ids"] for u in adoption["used_units"]):
                continue
            escalation_after = correction["escalate_at"] - correction["ack_deadline"]
            self._freeze_one(correction, adoption, correction["ack_deadline"], escalation_after)

    def request_version_switch(self, adoption_id: str, to_edition_id: str) -> dict:
        state = self.state()
        adoption = state.adoptions.get(adoption_id)
        if adoption is None:
            raise UnknownReference(f"采用关系不存在：{adoption_id}")
        target = state.editions.get(to_edition_id)
        if target is None:
            raise UnknownReference(f"目标版次不存在：{to_edition_id}")
        current_id = state.adopted_edition(adoption_id, self.clock.now())
        current = state.editions.get(current_id, {})
        if current.get("book_id") != target["book_id"]:
            raise DomainError("只能在同一教材的版次之间切换")
        return self._append(
            AGG_ADOPTION, adoption_id, EV_SWITCH_REQUESTED,
            {"from_edition_id": current_id, "to_edition_id": to_edition_id,
             "course": adoption["course"], "stage": adoption["stage"]},
            summary=f"申请切换 {adoption_id} 至 {to_edition_id}",
        )

    def complete_version_switch(
        self,
        adoption_id: str,
        to_edition_id: str,
        *,
        checked_course: bool,
        checked_stage: bool,
        checked_local_supplement: bool,
        local_supplement_id: str | None = None,
        local_supplement_hash: str | None = None,
    ) -> dict:
        """切换版本前必须逐层核对课程、学段、本地补充材料。"""
        state = self.state()
        adoption = state.adoptions.get(adoption_id)
        if adoption is None:
            raise UnknownReference(f"采用关系不存在：{adoption_id}")
        checks = {
            "course": checked_course,
            "stage": checked_stage,
            "local_supplement": checked_local_supplement,
        }
        missing = [name for name, ok in checks.items() if not ok]
        if missing:
            raise DomainError(f"切换版次前尚有层次未核对：{missing}")
        if to_edition_id not in state.editions:
            raise UnknownReference(f"目标版次不存在：{to_edition_id}")
        # 仍被重大勘误冻结的单元未解除前，不得整体切换。
        for freeze in state.freezes:
            if freeze["adoption_id"] != adoption_id:
                continue
            for unit_id in freeze["unit_ids"]:
                if not state.is_unit_released(freeze["correction_id"], adoption["school_id"], unit_id):
                    raise DomainError(f"单元 {unit_id} 仍处于冻结，不能切换版次")
        return self._append(
            AGG_ADOPTION, adoption_id, EV_SWITCH_COMPLETED,
            {"to_edition_id": to_edition_id,
             "checked_layers": list(SWITCH_LAYERS),
             "local_supplement_id": local_supplement_id,
             "local_supplement_hash": local_supplement_hash},
            summary=f"完成版次切换 {adoption_id} -> {to_edition_id}",
        )

    # ================= 问题与勘误 =================
    def report_issue(
        self,
        issue_id: str,
        *,
        unit_id: str,
        channel: str,
        edition_id: str,
        detail: str,
        discovered_at,
        suspected_severity: str = SEVERITY_NORMAL,
    ) -> dict:
        """迟到问题按实际发现时间入链；接收时间单独记录。"""
        discovered_at = as_utc(discovered_at)
        now = as_utc(self.clock.now())
        return self._append(
            AGG_ISSUE, issue_id, EV_ISSUE_REPORTED,
            {
                "unit_id": unit_id, "channel": channel, "edition_id": edition_id,
                "detail": detail, "suspected_severity": suspected_severity,
                "discovered_at": discovered_at.isoformat(),
                "received_at": now.isoformat(),
            },
            at=discovered_at,
            summary=f"问题入链（发现于 {discovered_at.date()}）：{detail[:20]}",
        )

    def approve_correction(
        self,
        correction_id: str,
        *,
        severity: str,
        unit_ids: list[str],
        channels: list[str],
        replacement: dict[str, dict],
        legal_basis_ids: list[str],
        issue_ids: list[str] | None = None,
        ack_deadline=None,
        escalation_after=DEFAULT_ESCALATION,
    ) -> dict:
        state = self.state()
        if correction_id in state.corrections:
            raise DomainError(f"勘误已存在：{correction_id}")
        if severity not in (SEVERITY_NORMAL, SEVERITY_MAJOR):
            raise DomainError(f"未知勘误级别：{severity}")
        for unit_id in unit_ids:
            if unit_id not in state.units:
                raise UnknownReference(f"内容单元不存在：{unit_id}")
        for channel in channels:
            if channel not in CHANNELS:
                raise DomainError(f"未知内容载体：{channel}")
            if channel not in replacement:
                raise DomainError(f"缺少 {channel} 的替换内容")
        for basis_id in legal_basis_ids:
            if basis_id not in state.legal:
                raise UnknownReference(f"法规依据不存在：{basis_id}")
        route = ROUTE_FREEZE if severity == SEVERITY_MAJOR else ROUTE_NEXT_EDITION
        now = as_utc(self.clock.now())
        deadline = as_utc(ack_deadline) if ack_deadline else now + DEFAULT_ACK_WINDOW
        event = self._append(
            AGG_CORRECTION, correction_id, EV_CORRECTION_APPROVED,
            {
                "severity": severity, "route": route,
                "unit_ids": list(unit_ids), "channels": list(channels),
                "replacement": replacement,
                "legal_basis_ids": list(legal_basis_ids),
                "issue_ids": list(issue_ids or ()),
                "ack_deadline": deadline.isoformat(),
                "escalate_at": (deadline + escalation_after).isoformat(),
            },
            summary=f"{'重大' if severity == SEVERITY_MAJOR else '普通'}勘误 {correction_id} 获批，处置：{route}",
        )
        if route == ROUTE_FREEZE:
            self._freeze_affected_adoptions(correction_id, deadline, escalation_after)
        # 普通勘误：若批准时恰有新版次在同事务稍后发布，由 release_edition 统一拾取；
        # 已发布版次永不回贴。
        return event

    def _freeze_affected_adoptions(self, correction_id: str, deadline, escalation_after) -> None:
        state = self.state()
        correction = state.corrections[correction_id]
        for adoption_id, adoption in state.adoptions.items():
            if not any(u in correction["unit_ids"] for u in adoption["used_units"]):
                continue
            self._freeze_one(correction, adoption, deadline, escalation_after)

    def _freeze_one(self, correction, adoption, deadline, escalation_after) -> None:
        correction_id = correction["correction_id"]
        self._append(
            AGG_CORRECTION, correction_id, EV_ADOPTION_FROZEN,
            {
                "adoption_id": adoption["adoption_id"],
                "school_id": adoption["school_id"],
                "school_name": adoption.get("school_name", ""),
                "course": adoption["course"], "stage": adoption["stage"],
                "unit_ids": [u for u in adoption["used_units"] if u in correction["unit_ids"]],
                "channels": list(correction["channels"]),
                "deadline": deadline.isoformat(),
            },
            summary=f"冻结 {adoption.get('school_name') or adoption['school_id']} 受影响内容",
        )
        reminder_id = f"reminder:{correction_id}:{adoption['adoption_id']}"
        self._append(
            AGG_REMINDER, reminder_id, EV_REMINDER_SCHEDULED,
            {
                "target_type": AGG_CORRECTION, "target_id": correction_id,
                "adoption_id": adoption["adoption_id"], "school_id": adoption["school_id"],
                "due_at": deadline.isoformat(),
                "escalate_at": (deadline + escalation_after).isoformat(),
            },
            summary=f"确认截止 {deadline.date()} / 升级 {(deadline + escalation_after).date()}",
        )

    # ================= 学校分层分批确认 =================
    def confirm_notice(
        self,
        correction_id: str,
        *,
        school_id: str,
        layer: str,
        unit_ids: list[str] | None = None,
    ) -> dict:
        if layer not in SWITCH_LAYERS:
            raise DomainError(f"未知核对层次：{layer}")
        state = self.state()
        correction = state.corrections.get(correction_id)
        if correction is None:
            raise UnknownReference(f"勘误不存在：{correction_id}")
        if correction["route"] != ROUTE_FREEZE:
            raise DomainError("普通勘误随下一版次生效，无需学校确认")
        freeze = state.freeze_for(correction_id, school_id)
        if freeze is None:
            raise UnknownReference("该校不在本勘误的冻结名单内")
        batch = list(unit_ids) if unit_ids else list(freeze["unit_ids"])
        unknown = [u for u in batch if u not in freeze["unit_ids"]]
        if unknown:
            raise DomainError(f"批次包含未冻结单元：{unknown}")

        prior = self._find_prior_confirm(state, correction_id, school_id, layer, batch)
        now = as_utc(self.clock.now())
        if prior is not None:
            if self._content_changed_after(state, correction_id, batch, prior["at"]):
                # 内容已变化：保留待核，不解除任何内容。
                held = self._append(
                    AGG_CORRECTION, correction_id, EV_NOTICE_ACKNOWLEDGED,
                    {"school_id": school_id, "layer": layer, "unit_ids": batch,
                     "result": ACK_HELD_CONTENT_CHANGED,
                     "original_ack_event_id": prior["ack_event_id"]},
                    at=now,
                    summary="再次确认时发现内容变化，保留待核",
                )
                return {"result": ACK_HELD_CONTENT_CHANGED, "event": held,
                        "original_ack_event_id": prior["ack_event_id"],
                        "released_unit_ids": []}
            return {"result": ACK_DUPLICATE, "event": None,
                    "original_ack_event_id": prior["ack_event_id"],
                    "confirmed_at": prior["at"].isoformat(),
                    "released_unit_ids": []}

        event = self._append(
            AGG_CORRECTION, correction_id, EV_NOTICE_ACKNOWLEDGED,
            {"school_id": school_id, "layer": layer, "unit_ids": batch,
             "result": ACK_CONFIRMED},
            at=now,
            summary=f"学校确认层次 {layer}，批次 {len(batch)} 个单元",
        )
        released = self._release_completed_units(correction_id, school_id, batch)
        return {"result": ACK_CONFIRMED, "event": event, "released_unit_ids": released}

    @staticmethod
    def _find_prior_confirm(state, correction_id, school_id, layer, batch):
        batch_set = set(batch)
        for ack in reversed(state.acks):
            if (
                ack["correction_id"] == correction_id
                and ack["school_id"] == school_id
                and ack["layer"] == layer
                and ack["result"] == ACK_CONFIRMED
                and batch_set <= set(ack["unit_ids"])
            ):
                return ack
        return None

    @staticmethod
    def _content_changed_after(state, correction_id, unit_ids, moment) -> bool:
        """是否有影响同批单元的更新勘误在原确认之后出现。"""
        for other_id, other in state.corrections.items():
            if other_id == correction_id:
                continue
            if other["approved_at"] <= moment:
                continue
            if set(other["unit_ids"]) & set(unit_ids):
                return True
        return False

    def _release_completed_units(self, correction_id: str, school_id: str, batch: list[str]) -> list[str]:
        """三层齐备的单元才解除；把替换内容按解除时刻挂入当前版次（区间保留历史）。"""
        state = self.state()
        correction = state.corrections[correction_id]
        freeze = state.freeze_for(correction_id, school_id)
        adoption = state.adoptions[freeze["adoption_id"]]
        now = as_utc(self.clock.now())
        released = []
        for unit_id in batch:
            if state.confirmed_layers(correction_id, school_id, unit_id) < set(SWITCH_LAYERS):
                continue
            current_edition = state.adopted_edition(freeze["adoption_id"], now)
            created_any = False
            for channel in correction["channels"]:
                already = any(
                    b.edition_id == current_edition
                    and b.channel == channel
                    and b.source_correction_id == correction_id
                    and b.from_at <= now
                    for b in state.units[unit_id]["bindings"]
                )
                if already:
                    continue
                replacement = correction["replacement"][channel]
                self._append(
                    AGG_UNIT, unit_id, EV_CONTENT_REFERENCE,
                    {
                        "unit_code": state.units[unit_id]["code"],
                        "title": state.units[unit_id].get("title", ""),
                        "edition_id": current_edition, "channel": channel,
                        "content_ref": replacement["content_ref"],
                        "content_hash": replacement["content_hash"],
                        "source_correction_id": correction_id,
                    },
                    at=now,
                    summary=f"三层确认齐备，解除单元 {unit_id}/{channel}",
                )
                state = self.state()
                created_any = True
            if created_any:
                released.append(unit_id)
        return released

    # ================= 教学事实（不可改写） =================
    def record_teaching_fact(
        self,
        fact_id: str,
        *,
        school_id: str,
        unit_id: str,
        channel: str,
        edition_id: str,
        taught_at,
        content_ref: str | None,
        content_hash: str,
        adoption_id: str | None = None,
    ) -> dict:
        taught_at = as_utc(taught_at)
        return self._append(
            AGG_TEACHING_FACT, fact_id, EV_TEACHING_FACT,
            {
                "school_id": school_id, "adoption_id": adoption_id,
                "unit_id": unit_id, "channel": channel,
                "edition_id": edition_id, "content_ref": content_ref,
                "content_hash": content_hash, "taught_at": taught_at.isoformat(),
            },
            at=taught_at,
            summary=f"教学事实：{school_id} 于 {taught_at.date()} 讲授 {unit_id}",
        )

    # ================= 逾期与升级提醒 =================
    def due_reminders(self):
        return self.state().due_reminders(self.clock.now())

    def dispatch_due_reminders(self) -> list[dict]:
        """按当前时钟派发；截止点固定在调度事件上，进程恢复后沿用。"""
        delivered = []
        for item in self.due_reminders():
            event = self._append(
                AGG_REMINDER, item["reminder_id"], EV_REMINDER_DELIVERED,
                {"reminder_id": item["reminder_id"], "level": item["level"],
                 "target_type": item["target_type"], "target_id": item["target_id"],
                 "school_id": item.get("school_id")},
                summary=f"派发{ {'due': '到期', 'overdue': '逾期', 'escalated': '升级'}[item['level']] }提醒",
            )
            delivered.append({"level": item["level"], "event": event, **{
                k: v for k, v in item.items() if k not in ("created_at", "sent_levels")
            }})
        return delivered

    # ================= 内部 =================
    def _append(self, aggregate_type, aggregate_id, event_type, payload, *, at=None,
                summary: str = "", expected_version: int | None = None) -> dict:
        moment = as_utc(at) if at is not None else as_utc(self.clock.now())
        if expected_version is None:
            expected_version = self.store.version_of(aggregate_type, aggregate_id)
        event_id = f"{aggregate_type}#{aggregate_id}#v{expected_version + 1}"
        return self.store.append(
            event_id=event_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=moment,
            payload=payload,
            expected_version=expected_version,
            summary=summary,
        )
