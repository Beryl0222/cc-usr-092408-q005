"""教材版本接续领域服务。

把教材版次、内容单元、法规依据、学校采用与勘误决定组织为**带有效区间的接续链**：
所有状态由只增事件按实际发生时间回放得到，任意历史日期都可还原当时文本。

硬约束：
- 安全性或教学影响重大的勘误（severity=safety/major）先冻结受影响内容，
  按 课程→学段→本地补充材料 三层等待学校分批确认，只解除相应内容；
- 普通勘误进入下个可用版次；
- 教学事实（TEACHING_OCCURRED 等）一经记录不可改写，任何后继处理只追加事件；
- 同一通知重复确认：内容一致返回原结果，内容变化转待核（PENDING_REVIEW），不覆盖原决定；
- 迟到问题按实际发现时间（found_at）入链，投影按 occurred_at 归位。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .clock import Clock, parse
from .event_store import Event, EventStore

# 聚合类型
AGG_EDITION = "textbook_edition"
AGG_UNIT = "content_unit"
AGG_REGULATION = "regulation"
AGG_ADOPTION = "course_adoption"
AGG_NOTICE = "correction_notice"
AGG_FREEZE_ACK = "freeze_acknowledgement"
AGG_SCHOOL = "school"
AGG_TEACHING = "teaching_record"
AGG_PREP = "teacher_prep_reference"

# 事件类型
EDITION_REGISTERED = "EDITION_REGISTERED"
EDITION_RELEASED = "EDITION_RELEASED"
EDITION_RETIRED = "EDITION_RETIRED"
PRINT_BATCH_REGISTERED = "PRINT_BATCH_REGISTERED"
UNIT_REGISTERED = "UNIT_REGISTERED"
LAW_REGISTERED = "LAW_REGISTERED"
LAW_BASIS_ATTACHED = "LAW_BASIS_ATTACHED"
SCHOOL_REGISTERED = "SCHOOL_REGISTERED"
ADOPTION_STARTED = "ADOPTION_STARTED"
ADOPTION_SWITCHED = "ADOPTION_SWITCHED"
ISSUE_REPORTED = "ISSUE_REPORTED"
CORRECTION_DECIDED = "CORRECTION_DECIDED"          # 普通勘误→入下版
FREEZE_IMPOSED = "FREEZE_IMPOSED"                  # 重大勘误→冻结
DECISION_REVIEW_PENDING = "DECISION_REVIEW_PENDING"  # 内容变化→待核
CORRECTION_INCLUDED = "CORRECTION_INCLUDED"        # 勘误已进入某可用版次
FREEZE_ACK_LAYER = "FREEZE_ACK_LAYER"              # 学校分批分层确认
CONTENT_TAUGHT = "CONTENT_TAUGHT"                  # 教学事实（不可变）
PREP_REFERENCED = "PREP_REFERENCED"                # 教师备课引用事实（不可变）

LAYER_COURSE = "course"
LAYER_STAGE = "stage"
LAYER_SUPPLEMENT = "supplement"
ALL_LAYERS = (LAYER_COURSE, LAYER_STAGE, LAYER_SUPPLEMENT)

SEVERITY_NORMAL = "normal"
SEVERITY_SAFETY = "safety"
SEVERITY_MAJOR = "major"
FREEZE_SEVERITIES = (SEVERITY_SAFETY, SEVERITY_MAJOR)


class BusinessError(ValueError):
    """业务规则被违反。"""


def adoption_id(school_id: str, course: str) -> str:
    return f"adoption:{school_id}:{course}"


def freeze_ack_id(notice_id: str, school_id: str) -> str:
    return f"freezeack:{notice_id}:{school_id}"


# ----------------------------------------------------------------------
# 读模型（由事件回放重建）
# ----------------------------------------------------------------------
@dataclass
class UnitView:
    unit_id: str
    edition_id: str
    code: str
    title: str
    body_hash: str
    ordinal: int
    effective_from: datetime
    effective_to: datetime | None = None
    notice_ids: list[str] = field(default_factory=list)


@dataclass
class EditionView:
    edition_id: str
    course: str
    stage: str
    title: str
    publisher: str
    state: str = "registered"
    registered_at: datetime | None = None
    released_at: datetime | None = None
    retired_at: datetime | None = None
    law_refs: dict[str, datetime] = field(default_factory=dict)   # regulation_id -> 挂载时刻
    print_batches: dict[str, datetime] = field(default_factory=dict)
    included_notices: list[str] = field(default_factory=list)


@dataclass
class AdoptionInterval:
    edition_id: str
    start: datetime
    end: datetime | None = None
    form: str = "electronic"
    switched_by: str | None = None


@dataclass
class UnitConfirm:
    layers: set[str] = field(default_factory=set)

    @property
    def released(self) -> bool:
        return set(ALL_LAYERS).issubset(self.layers)


class Projection:
    """按 occurred_at 全量回放事件得到的有效区间读模型。"""

    def __init__(self, store: EventStore) -> None:
        self.editions: dict[str, EditionView] = {}
        self.units: dict[tuple[str, str], UnitView] = {}   # (edition_id, unit code) -> 当前单元
        self.unit_versions: list[UnitView] = []
        self.regulations: dict[str, dict] = {}
        self.schools: dict[str, dict] = {}
        self.adoptions: dict[str, dict] = {}
        self.notices: dict[str, dict] = {}
        self.acks: dict[str, dict[str, dict[str, UnitConfirm]]] = {}
        # ack 聚合：notice -> school -> unit_id -> 确认状态
        self.ack_layers: dict[str, dict[str, set[str]]] = {}
        self.teachings: list[dict] = []
        self.preps: list[dict] = []
        for e in store.chronological():
            self._apply(e)

    # ---- 回放 ----------------------------------------------------------
    def _apply(self, e: Event) -> None:
        p = e.payload
        t = e.event_type
        if t == EDITION_REGISTERED:
            self.editions[e.aggregate_id] = EditionView(
                edition_id=e.aggregate_id,
                course=p["course"], stage=p["stage"],
                title=p["title"], publisher=p["publisher"],
                state="registered", registered_at=e.occurred_at,
            )
        elif t == EDITION_RELEASED:
            ed = self.editions[e.aggregate_id]
            ed.state = "released"
            ed.released_at = e.occurred_at
        elif t == EDITION_RETIRED:
            ed = self.editions[e.aggregate_id]
            ed.state = "retired"
            ed.retired_at = e.occurred_at
        elif t == PRINT_BATCH_REGISTERED:
            self.editions[e.aggregate_id].print_batches[p["batch_id"]] = parse(p["printed_at"])
        elif t == UNIT_REGISTERED:
            key = (p["edition_id"], p["code"])
            old = self.units.get(key)
            if old is not None:
                old.effective_to = e.occurred_at
            view = UnitView(
                unit_id=p["unit_id"], edition_id=p["edition_id"], code=p["code"],
                title=p["title"], body_hash=p["body_hash"], ordinal=p["ordinal"],
                effective_from=e.occurred_at, notice_ids=list(p.get("notice_ids", [])),
            )
            self.units[key] = view
            self.unit_versions.append(view)
        elif t == LAW_REGISTERED:
            self.regulations[e.aggregate_id] = {
                "regulation_id": e.aggregate_id, "title": p["title"],
                "article": p["article"], "effective_from": parse(p["effective_from"]),
                "valid_to": None,
            }
        elif t == LAW_BASIS_ATTACHED:
            self.editions[p["edition_id"]].law_refs[p["regulation_id"]] = e.occurred_at
        elif t == SCHOOL_REGISTERED:
            self.schools[e.aggregate_id] = {"school_id": e.aggregate_id, "name": p["name"]}
        elif t == ADOPTION_STARTED:
            self.adoptions[e.aggregate_id] = {
                "adoption_id": e.aggregate_id,
                "school_id": p["school_id"], "course": p["course"], "stage": p["stage"],
                "supplement_hashes": list(p.get("supplement_hashes", [])),
                "intervals": [AdoptionInterval(
                    edition_id=p["edition_id"], start=e.occurred_at, form=p.get("form", "electronic"))],
            }
        elif t == ADOPTION_SWITCHED:
            a = self.adoptions[e.aggregate_id]
            a["intervals"][-1].end = e.occurred_at
            a["intervals"].append(AdoptionInterval(
                edition_id=p["target_edition_id"], start=e.occurred_at,
                form=p.get("form", a["intervals"][-1].form), switched_by=e.event_id,
            ))
        elif t == ISSUE_REPORTED:
            self.notices[e.aggregate_id] = {
                "notice_id": e.aggregate_id, "unit_id": p["unit_id"],
                "edition_id": p["edition_id"], "severity": p["severity"],
                "description": p["description"], "found_at": parse(p["found_at"]),
                "reported_event": e.event_id, "state": "reported",
                "decision_event": None, "decision": None, "law_refs": [],
                "replacement_edition_id": None, "replacement_unit_id": None,
                "deadline": None, "included_edition_id": None,
                "review_pending": [],
            }
        elif t == CORRECTION_DECIDED:
            n = self.notices[e.aggregate_id]
            n.update(state="queued", decision=p["decision"], decision_event=e.event_id,
                     decided_at=e.occurred_at, law_refs=list(p.get("law_refs", [])))
        elif t == FREEZE_IMPOSED:
            n = self.notices[e.aggregate_id]
            n.update(state="frozen", decision="freeze", decision_event=e.event_id,
                     decided_at=e.occurred_at, law_refs=list(p.get("law_refs", [])),
                     replacement_edition_id=p.get("replacement_edition_id"),
                     replacement_unit_id=p.get("replacement_unit_id"),
                     deadline=parse(p["deadline"]) if p.get("deadline") else None,
                     affected_units=list(p.get("affected_units", [n["unit_id"]])))
        elif t == DECISION_REVIEW_PENDING:
            self.notices[e.aggregate_id]["review_pending"].append({
                "event_id": e.event_id, "at": e.occurred_at, "proposal": p.get("proposal", {}),
            })
        elif t == CORRECTION_INCLUDED:
            n = self.notices[e.aggregate_id]
            n["state"] = "included"
            n["included_edition_id"] = p["edition_id"]
            self.editions[p["edition_id"]].included_notices.append(e.aggregate_id)
        elif t == FREEZE_ACK_LAYER:
            per_school = self.acks.setdefault(e.aggregate_id, {})
            # aggregate_id 是学校×通知维度；layers 载荷里可以多单元多分层
            school = p["school_id"]
            unit_map = per_school.setdefault(school, {})
            for unit_id in p["unit_ids"]:
                unit_map.setdefault(unit_id, UnitConfirm()).layers.update(p["layers"])
            self.ack_layers.setdefault(p["notice_id"], {}).setdefault(school, set()).update(p["layers"])
        elif t == CONTENT_TAUGHT:
            self.teachings.append({
                "teaching_id": e.aggregate_id, "school_id": p["school_id"],
                "unit_id": p["unit_id"], "edition_id": p["edition_id"],
                "taught_at": parse(p["taught_at"]), "content_hash": p["content_hash"],
                "event_id": e.event_id,
            })
        elif t == PREP_REFERENCED:
            self.preps.append({
                "school_id": p["school_id"], "unit_id": p["unit_id"],
                "edition_id": p["edition_id"], "referenced_at": parse(p["referenced_at"]),
                "content_hash": p["content_hash"], "event_id": e.event_id,
            })

    # ---- 查询 ----------------------------------------------------------
    def edition_at(self, adoption: dict, at: datetime) -> AdoptionInterval | None:
        for iv in adoption["intervals"]:
            if iv.start <= at and (iv.end is None or at < iv.end):
                return iv
        return None

    def unit_at(self, edition_id: str, code: str, at: datetime) -> UnitView | None:
        for v in self.unit_versions:
            if v.edition_id == edition_id and v.code == code and v.effective_from <= at \
                    and (v.effective_to is None or at < v.effective_to):
                return v
        return None

    def notice_for_unit(self, unit_id: str, at: datetime) -> dict | None:
        """该单元在 at 时刻处于生效中的勘误（冻结优先）。"""
        hit = None
        for n in self.notices.values():
            affected = set(n.get("affected_units", [n["unit_id"]]))
            if unit_id not in affected:
                continue
            decided = n.get("decided_at")
            if decided and decided <= at and n["state"] in ("frozen", "queued", "included"):
                if n["state"] == "frozen":
                    return n
                hit = n
        return hit

    def is_released_for(self, notice_id: str, school_id: str, unit_id: str) -> bool:
        try:
            return self.acks[freeze_ack_id(notice_id, school_id)][school_id][unit_id].released
        except KeyError:
            return False

    def confirm_progress(self, notice_id: str, school_id: str, unit_ids: list[str]) -> dict:
        out: dict[str, list[str]] = {}
        for unit_id in unit_ids:
            try:
                done = set(self.acks[freeze_ack_id(notice_id, school_id)][school_id][unit_id].layers)
            except KeyError:
                done = set()
            out[unit_id] = [layer for layer in ALL_LAYERS if layer in done]
        return out


class TextbookService:
    """命令侧：校验业务规则并追加事件。"""

    def __init__(self, store: EventStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    # ---- 内部工具 ------------------------------------------------------
    def _next(self, aggregate_type: str, aggregate_id: str, event_type: str,
              occurred_at: datetime, payload: dict, summary: str,
              causation_id: str | None = None, event_id: str | None = None) -> Event:
        version = self.store.version_of(aggregate_id) + 1
        event = Event(
            event_id=event_id or f"{aggregate_id}-v{version}",
            event_type=event_type, aggregate_type=aggregate_type,
            aggregate_id=aggregate_id, occurred_at=occurred_at, version=version,
            summary=summary, payload=payload, causation_id=causation_id,
        )
        return self.store.append(event)

    def _project(self) -> Projection:
        return Projection(self.store)

    # ---- 学校与法规 ----------------------------------------------------
    def register_school(self, school_id: str, name: str, *, event_id: str | None = None) -> Event:
        if school_id in self._project().schools:
            raise BusinessError(f"学校已登记：{school_id}")
        return self._next(AGG_SCHOOL, school_id, SCHOOL_REGISTERED, self.clock.now(),
                          {"name": name}, f"登记学校 {name}", event_id=event_id)

    def register_regulation(self, regulation_id: str, title: str, article: str,
                            effective_from: str | datetime, *,
                            event_id: str | None = None) -> Event:
        at = self.clock.now()
        return self._next(AGG_REGULATION, regulation_id, LAW_REGISTERED, at,
                          {"title": title, "article": article,
                           "effective_from": parse(effective_from).isoformat()},
                          f"登记法规依据 {title} {article}", event_id=event_id)

    # ---- 教材版次与内容单元 --------------------------------------------
    def register_edition(self, edition_id: str, *, course: str, stage: str, title: str,
                         publisher: str, law_refs: tuple[str, ...] = (),
                         event_id: str | None = None) -> Event:
        if edition_id in self._project().editions:
            raise BusinessError(f"版次已登记：{edition_id}")
        event = self._next(AGG_EDITION, edition_id, EDITION_REGISTERED, self.clock.now(),
                           {"course": course, "stage": stage, "title": title,
                            "publisher": publisher, "law_refs": list(law_refs)},
                           f"登记版次 {title}（{course}/{stage}）", event_id=event_id)
        for reg_id in law_refs:
            self._attach_law(edition_id, reg_id)
        return event

    def _attach_law(self, edition_id: str, regulation_id: str) -> Event:
        proj = self._project()
        if regulation_id not in proj.regulations:
            raise BusinessError(f"法规依据不存在：{regulation_id}")
        return self._next(AGG_EDITION, edition_id, LAW_BASIS_ATTACHED, self.clock.now(),
                          {"edition_id": edition_id, "regulation_id": regulation_id},
                          f"版次 {edition_id} 挂载法规 {regulation_id}")

    def release_edition(self, edition_id: str, *, includes_notice_ids: tuple[str, ...] = (),
                        event_id: str | None = None) -> Event:
        proj = self._project()
        if edition_id not in proj.editions:
            raise BusinessError(f"版次不存在：{edition_id}")
        if proj.editions[edition_id].state == "released":
            raise BusinessError(f"版次已发布：{edition_id}")
        event = self._next(AGG_EDITION, edition_id, EDITION_RELEASED, self.clock.now(),
                           {"includes_notice_ids": list(includes_notice_ids)},
                           f"发布版次 {edition_id} 为可用版次", event_id=event_id)
        for notice_id in includes_notice_ids:
            self._mark_included(notice_id, edition_id)
        return event

    def _mark_included(self, notice_id: str, edition_id: str) -> Event:
        proj = self._project()
        notice = proj.notices.get(notice_id)
        if notice is None:
            raise BusinessError(f"勘误通知不存在：{notice_id}")
        if notice["state"] not in ("queued", "reported"):
            raise BusinessError(
                f"勘误 {notice_id} 状态为 {notice['state']}，不能随版次发布")
        return self._next(AGG_NOTICE, notice_id, CORRECTION_INCLUDED, self.clock.now(),
                          {"edition_id": edition_id},
                          f"普通勘误 {notice_id} 进入可用版次 {edition_id}")

    def register_print_batch(self, edition_id: str, batch_id: str, printed_at: str | datetime,
                             *, quantity: int = 0, event_id: str | None = None) -> Event:
        proj = self._project()
        if edition_id not in proj.editions:
            raise BusinessError(f"版次不存在：{edition_id}")
        if batch_id in proj.editions[edition_id].print_batches:
            raise BusinessError(f"印刷批次已存在：{batch_id}")
        return self._next(AGG_EDITION, edition_id, PRINT_BATCH_REGISTERED, self.clock.now(),
                          {"batch_id": batch_id, "printed_at": parse(printed_at).isoformat(),
                           "quantity": quantity},
                          f"登记在印批次 {batch_id}", event_id=event_id)

    def register_unit(self, unit_id: str, edition_id: str, *, code: str, title: str,
                      body_hash: str, ordinal: int, notice_ids: tuple[str, ...] = (),
                      at: str | datetime | None = None,
                      event_id: str | None = None) -> Event:
        proj = self._project()
        if edition_id not in proj.editions:
            raise BusinessError(f"版次不存在：{edition_id}")
        if (edition_id, code) in proj.units:
            raise BusinessError(f"版次 {edition_id} 内单元编码已存在：{code}")
        when = parse(at) if at else self.clock.now()
        return self._next(AGG_UNIT, unit_id, UNIT_REGISTERED, when,
                          {"unit_id": unit_id, "edition_id": edition_id, "code": code,
                           "title": title, "body_hash": body_hash, "ordinal": ordinal,
                           "notice_ids": list(notice_ids)},
                          f"登记内容单元 {code}《{title}》", event_id=event_id)

    # ---- 学校采用 ------------------------------------------------------
    def adopt(self, school_id: str, edition_id: str, *, course: str, stage: str,
              supplement_hashes: tuple[str, ...] = (), form: str = "electronic",
              at: str | datetime | None = None, event_id: str | None = None) -> Event:
        proj = self._project()
        if school_id not in proj.schools:
            raise BusinessError(f"学校未登记：{school_id}")
        edition = proj.editions.get(edition_id)
        if edition is None:
            raise BusinessError(f"版次不存在：{edition_id}")
        if edition.state != "released":
            raise BusinessError(f"版次尚未发布，不可采用：{edition_id}")
        if edition.course != course or edition.stage != stage:
            raise BusinessError(
                f"采用核对失败：版次为 {edition.course}/{edition.stage}，"
                f"申报为 {course}/{stage}")
        agg = adoption_id(school_id, course)
        if agg in proj.adoptions:
            raise BusinessError(f"该校该课程已有采用链，切换请用 confirm_switch：{agg}")
        when = parse(at) if at else self.clock.now()
        return self._next(AGG_ADOPTION, agg, ADOPTION_STARTED, when,
                          {"school_id": school_id, "course": course, "stage": stage,
                           "edition_id": edition_id,
                           "supplement_hashes": list(supplement_hashes), "form": form},
                          f"学校 {school_id} 采用 {edition_id}（{form}）", event_id=event_id)

    def confirm_switch(self, school_id: str, course: str, target_edition_id: str, *,
                       checked_course: str, checked_stage: str,
                       checked_supplement_hashes: tuple[str, ...],
                       form: str | None = None, at: str | datetime | None = None,
                       event_id: str | None = None) -> Event:
        """切换版次：必须逐项核对课程、学段、本地补充材料；冻结内容须已分批解除。"""
        proj = self._project()
        agg = adoption_id(school_id, course)
        adoption = proj.adoptions.get(agg)
        if adoption is None:
            raise BusinessError(f"采用链不存在：{agg}")
        target = proj.editions.get(target_edition_id)
        if target is None or target.state != "released":
            raise BusinessError(f"目标版次不存在或未发布：{target_edition_id}")
        if checked_course != course or checked_course != target.course:
            raise BusinessError("课程核对不一致，拒绝切换")
        if checked_stage != adoption["stage"] or checked_stage != target.stage:
            raise BusinessError("学段核对不一致，拒绝切换")
        if set(checked_supplement_hashes) != set(adoption["supplement_hashes"]):
            raise BusinessError("本地补充材料核对不一致，拒绝切换")
        # 受冻结影响的内容只有已分批确认的才解除；
        # 无论切向哪个版次，当前版次上未解除的冻结都必须先完成确认
        current_edition_id = adoption["intervals"][-1].edition_id
        remaining = self._frozen_units_for(
            proj, school_id, {current_edition_id, target_edition_id})
        if remaining:
            raise BusinessError(f"仍有受冻结内容未完成确认：{sorted(remaining)}")
        when = parse(at) if at else self.clock.now()
        payload = {"school_id": school_id, "course": course,
                   "target_edition_id": target_edition_id,
                   "checked": {"course": checked_course, "stage": checked_stage,
                               "supplement_hashes": list(checked_supplement_hashes)}}
        if form:
            payload["form"] = form
        return self._next(AGG_ADOPTION, agg, ADOPTION_SWITCHED, when, payload,
                          f"学校 {school_id} 经三层核对切换至 {target_edition_id}",
                          event_id=event_id)

    def _frozen_units_for(self, proj: Projection, school_id: str,
                          edition_ids: set[str] | str) -> set[str]:
        edition_ids = {edition_ids} if isinstance(edition_ids, str) else set(edition_ids)
        remaining: set[str] = set()
        for n in proj.notices.values():
            if n["state"] != "frozen" or n["edition_id"] not in edition_ids:
                continue
            for unit_id in n.get("affected_units", [n["unit_id"]]):
                if not proj.is_released_for(n["notice_id"], school_id, unit_id):
                    remaining.add(unit_id)
        return remaining

    # ---- 勘误 ----------------------------------------------------------
    def report_issue(self, notice_id: str, unit_id: str, edition_id: str, *,
                     severity: str, description: str,
                     found_at: str | datetime, event_id: str | None = None) -> Event:
        """登记问题。迟到问题按 *实际发现时间* found_at 入链。"""
        proj = self._project()
        if unit_id not in {u.unit_id for u in proj.unit_versions}:
            raise BusinessError(f"内容单元不存在：{unit_id}")
        if notice_id in proj.notices:
            raise BusinessError(f"勘误通知已存在：{notice_id}")
        if severity not in (SEVERITY_NORMAL, SEVERITY_SAFETY, SEVERITY_MAJOR):
            raise BusinessError(f"未知严重程度：{severity}")
        found = parse(found_at)
        if found > self.clock.now():
            raise BusinessError("发现时间不能晚于当前时钟")
        # 迟到问题按“实际发现时间”入链（事件 occurred_at=found_at），
        # 登记时刻另存 recorded_at；投影按 occurred_at 回放归位。
        return self._next(AGG_NOTICE, notice_id, ISSUE_REPORTED, found,
                          {"unit_id": unit_id, "edition_id": edition_id,
                           "severity": severity, "description": description,
                           "found_at": found.isoformat(),
                           "recorded_at": self.clock.now().isoformat()},
                          f"登记勘误（{severity}）：{description}", event_id=event_id)

    def decide_correction(self, notice_id: str, *, decision: str | None = None,
                          law_refs: tuple[str, ...] = (),
                          replacement_edition_id: str | None = None,
                          replacement_unit_id: str | None = None,
                          deadline: str | datetime | None = None,
                          affected_units: tuple[str, ...] = (),
                          at: str | datetime | None = None,
                          event_id: str | None = None) -> Event:
        """对勘误作出处理决定。

        - safety/major：decision="freeze"，立即冻结受影响内容并设置确认截止点；
        - normal：进入下个可用版次（发布版次时收录）。
        重复提交：内容一致→返回原决定事件；内容变化→转待核，原决定继续有效。
        """
        proj = self._project()
        notice = proj.notices.get(notice_id)
        if notice is None:
            raise BusinessError(f"勘误通知不存在：{notice_id}")
        when = parse(at) if at else self.clock.now()
        normalized_decision = (
            "freeze" if notice["severity"] in FREEZE_SEVERITIES
            else (decision or "queue_next_edition"))
        proposal = {"decision": normalized_decision, "law_refs": list(law_refs),
                    "replacement_edition_id": replacement_edition_id,
                    "replacement_unit_id": replacement_unit_id,
                    "deadline": parse(deadline).isoformat() if deadline else None,
                    "affected_units": list(affected_units)}

        if notice["state"] in ("frozen", "queued", "included"):
            original = self.store.stream(notice_id)
            decided = next(e for e in original if e.event_type in (FREEZE_IMPOSED, CORRECTION_DECIDED))
            if _same_decision(decided.payload, proposal, notice["severity"]):
                return decided  # 同一通知再次确认 → 返回原结果
            # 内容变化 → 保留待核，不改写原决定
            self._next(AGG_NOTICE, notice_id, DECISION_REVIEW_PENDING, when,
                       {"proposal": proposal},
                       f"勘误 {notice_id} 重复确认内容变化，转待核",
                       causation_id=decided.event_id, event_id=event_id)
            raise BusinessError(
                f"勘误 {notice_id} 已有生效决定，新内容不一致，已登记待核（PENDING_REVIEW）")

        if notice["severity"] in FREEZE_SEVERITIES:
            if decision is not None and decision != "freeze":
                raise BusinessError("安全/教学影响重大勘误必须先冻结（decision=freeze）")
            if deadline is None:
                raise BusinessError("冻结决定必须给出学校确认截止时间 deadline")
            payload = {"decision": "freeze", "law_refs": list(law_refs),
                       "affected_units": list(affected_units) or [notice["unit_id"]],
                       "replacement_edition_id": replacement_edition_id,
                       "replacement_unit_id": replacement_unit_id,
                       "deadline": parse(deadline).isoformat()}
            return self._next(AGG_NOTICE, notice_id, FREEZE_IMPOSED, when, payload,
                              f"重大勘误 {notice_id}：冻结受影响内容并等待学校确认",
                              event_id=event_id)
        payload = {"decision": normalized_decision, "law_refs": list(law_refs)}
        return self._next(AGG_NOTICE, notice_id, CORRECTION_DECIDED, when, payload,
                          f"普通勘误 {notice_id} 进入下个可用版次", event_id=event_id)

    def acknowledge_freeze(self, notice_id: str, school_id: str, *,
                           unit_ids: tuple[str, ...], layers: tuple[str, ...],
                           at: str | datetime | None = None,
                           event_id: str | None = None) -> Event:
        """学校分批分层确认冻结内容；每批只解除对应 unit 的相应分层。"""
        proj = self._project()
        notice = proj.notices.get(notice_id)
        if notice is None or notice["state"] != "frozen":
            raise BusinessError(f"勘误不存在或未处于冻结状态：{notice_id}")
        invalid = set(layers) - set(ALL_LAYERS)
        if invalid:
            raise BusinessError(f"未知核对层：{sorted(invalid)}")
        affected = set(notice.get("affected_units", [notice["unit_id"]]))
        extra = set(unit_ids) - affected
        if extra:
            raise BusinessError(f"确认单元不在冻结范围内：{sorted(extra)}")
        agg = freeze_ack_id(notice_id, school_id)
        # 幂等：同一批 unit+layer 重复确认，返回最近一条原确认事件
        for e in reversed(self.store.stream(agg)):
            if set(e.payload["unit_ids"]) == set(unit_ids) and set(e.payload["layers"]) == set(layers):
                return e
        when = parse(at) if at else self.clock.now()
        return self._next(AGG_FREEZE_ACK, agg, FREEZE_ACK_LAYER, when,
                          {"notice_id": notice_id, "school_id": school_id,
                           "unit_ids": list(unit_ids), "layers": list(layers)},
                          f"学校 {school_id} 确认 {list(layers)}：{list(unit_ids)}",
                          causation_id=notice["decision_event"], event_id=event_id)

    # ---- 教学事实（不可变） --------------------------------------------
    def record_teaching(self, teaching_id: str, school_id: str, unit_id: str,
                        edition_id: str, taught_at: str | datetime, content_hash: str, *,
                        event_id: str | None = None) -> Event:
        """记录已经发生的教学事实（允许事后补录）。

        任何勘误/冻结/切换都不会改写本记录；taught_at 是事实发生时刻，
        事件入链时刻为当前时钟，二者解耦。
        """
        when = parse(taught_at)
        # 教学事实按其实际发生时间入链，任何后续处理只能追加事件，不能改写本记录。
        return self._next(AGG_TEACHING, teaching_id, CONTENT_TAUGHT, when,
                          {"school_id": school_id, "unit_id": unit_id,
                           "edition_id": edition_id, "taught_at": when.isoformat(),
                           "recorded_at": self.clock.now().isoformat(),
                           "content_hash": content_hash},
                          f"记录教学事实：{school_id} 于 {when.date()} 讲授 {unit_id}",
                          event_id=event_id)

    def record_prep_reference(self, school_id: str, unit_id: str, edition_id: str,
                              referenced_at: str | datetime, content_hash: str, *,
                              event_id: str | None = None) -> Event:
        """记录教师备课引用（不可变事实）。"""
        when = parse(referenced_at)
        return self._next(AGG_PREP, f"prep:{school_id}:{unit_id}:{when.isoformat()}",
                          PREP_REFERENCED, when,
                          {"school_id": school_id, "unit_id": unit_id,
                           "edition_id": edition_id, "referenced_at": when.isoformat(),
                           "recorded_at": self.clock.now().isoformat(),
                           "content_hash": content_hash},
                          f"记录备课引用：{school_id} 引用 {edition_id}/{unit_id}",
                          event_id=event_id)

    # ---- 历史还原 ------------------------------------------------------
    def resolve_text(self, school_id: str, course: str, code: str,
                     at: str | datetime) -> dict:
        """还原某学校某课程在任意历史日期 *应当使用* 的教材内容。

        返回状态：active（正常使用）/ frozen（受冻结影响，学校尚未解除该内容）。
        """
        proj = self._project()
        when = parse(at)
        adoption = proj.adoptions.get(adoption_id(school_id, course))
        if adoption is None:
            return {"status": "no_adoption", "at": when.isoformat()}
        interval = proj.edition_at(adoption, when)
        if interval is None:
            return {"status": "not_adopted_yet", "at": when.isoformat()}
        edition = proj.editions[interval.edition_id]
        unit = proj.unit_at(interval.edition_id, code, when)
        if unit is None:
            return {"status": "unit_not_found", "edition_id": edition.edition_id,
                    "at": when.isoformat()}
        notice = proj.notice_for_unit(unit.unit_id, when)
        result = {
            "at": when.isoformat(), "status": "active",
            "school_id": school_id, "course": course,
            "edition_id": edition.edition_id, "form": interval.form,
            "unit_id": unit.unit_id, "unit_code": unit.code, "title": unit.title,
            "body_hash": unit.body_hash,
            "law_refs": [
                {"regulation_id": rid,
                 "title": proj.regulations.get(rid, {}).get("title"),
                 "article": proj.regulations.get(rid, {}).get("article")}
                for rid in edition.law_refs
            ],
            "print_batches": {bid: at.isoformat()
                              for bid, at in edition.print_batches.items()},
        }
        if notice is not None and notice["state"] == "frozen":
            released = proj.is_released_for(notice["notice_id"], school_id, unit.unit_id)
            if not released:
                result["status"] = "frozen"
                result["notice_id"] = notice["notice_id"]
                result["deadline"] = notice["deadline"].isoformat() if notice["deadline"] else None
                result["confirm_progress"] = proj.confirm_progress(
                    notice["notice_id"], school_id, [unit.unit_id])[unit.unit_id]
        return result

    def teaching_facts(self, school_id: str, course: str | None = None) -> list[dict]:
        """调出已发生且永不变更的教学事实。"""
        proj = self._project()
        facts = [dict(t) for t in proj.teachings if t["school_id"] == school_id]
        if course is not None:
            editions = {eid for eid, v in proj.editions.items() if v.course == course}
            facts = [t for t in facts if t["edition_id"] in editions]
        return sorted(facts, key=lambda t: t["taught_at"])

    # ---- 监管视图 ------------------------------------------------------
    def regulatory_trace(self, notice_id: str) -> dict:
        """从一条替换/勘误记录出发的完整血缘视图。"""
        proj = self._project()
        notice = proj.notices.get(notice_id)
        if notice is None:
            raise BusinessError(f"勘误通知不存在：{notice_id}")
        events = self.store.stream(notice_id)
        affected_units = notice.get("affected_units", [notice["unit_id"]])

        blocked: list[dict] = []
        for adoption in proj.adoptions.values():
            school_id = adoption["school_id"]
            # 采用链（历史或当前）覆盖过受影响版次的学校都在追溯范围内
            if not any(iv.edition_id == notice["edition_id"] for iv in adoption["intervals"]):
                continue
            progress = proj.confirm_progress(notice_id, school_id, list(affected_units))
            unreleased = [u for u, layers in progress.items()
                          if set(layers) != set(ALL_LAYERS)]
            # 冻结且学校从未确认时，所有受影响单元均待确认
            if not progress and notice["state"] == "frozen":
                unreleased = list(affected_units)
            current = proj.edition_at(adoption, self.clock.now())
            blocked.append({
                "school_id": school_id, "course": adoption["course"],
                "current_edition_id": current.edition_id if current else None,
                "confirmed_layers_by_unit": progress,
                "unreleased_units": unreleased,
                "still_blocked": bool(unreleased) and notice["state"] == "frozen",
            })

        law_basis = []
        for rid in notice.get("law_refs", []):
            reg = proj.regulations.get(rid, {})
            law_basis.append({"regulation_id": rid, "title": reg.get("title"),
                              "article": reg.get("article"),
                              "effective_from": reg.get("effective_from").isoformat()
                              if reg.get("effective_from") else None})
        decision_version = next(
            (e.version for e in events if e.event_id == notice.get("decision_event")), None)
        return {
            "notice_id": notice_id,
            "severity": notice["severity"],
            "state": notice["state"],
            "description": notice["description"],
            "found_at": notice["found_at"].isoformat(),
            "decided_at": notice.get("decided_at").isoformat() if notice.get("decided_at") else None,
            "decision_event_id": notice.get("decision_event"),
            "decision_event_version": decision_version,
            "affected_edition_id": notice["edition_id"],
            "affected_units": affected_units,
            "replacement": {
                "edition_id": notice.get("replacement_edition_id"),
                "unit_id": notice.get("replacement_unit_id"),
                "included_edition_id": notice.get("included_edition_id"),
            },
            "law_basis": law_basis,
            "event_chain": [
                {"event_id": e.event_id, "event_type": e.event_type,
                 "occurred_at": e.occurred_at.isoformat(), "version": e.version}
                for e in events
            ],
            "review_pending": notice.get("review_pending", []),
            "blocked_schools": sorted(blocked, key=lambda b: b["school_id"]),
        }


def _same_decision(decided_payload: dict, proposal: dict, severity: str) -> bool:
    if severity in FREEZE_SEVERITIES:
        keys = ("decision", "law_refs", "replacement_edition_id",
                "replacement_unit_id", "deadline", "affected_units")
    else:
        keys = ("decision", "law_refs")
    for key in keys:
        a = decided_payload.get(key)
        b = proposal.get(key)
        if isinstance(a, list):
            if sorted(a) != sorted(b or []):
                return False
        elif a != b:
            return False
    return True
