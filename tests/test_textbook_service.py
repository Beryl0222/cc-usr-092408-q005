"""教材版本接续服务的端到端领域测试。

覆盖：
1. 版次/印刷批次/电子版/备课引用可区分，任意历史日期还原当时文本；
2. 重大勘误先冻结、三层分批确认、只解除相应内容；
3. 普通勘误进入下个可用版次；
4. 教学事实在任何后续处理下不变；
5. 切换前核对课程/学段/本地补充材料；
6. 迟到问题按实际发现时间入链；
7. 同一通知重复确认：一致返回原结果，变化保留待核；
8. 注入时钟 + 进程恢复后沿用原截止点；
9. 监管血缘视图：受阻学校、各层确认进度、法规与勘误版本依据。
"""
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile

from src import (
    FixedClock, EventStore, TextbookService, ReminderService, BusinessError,
    LAYER_COURSE, LAYER_STAGE, LAYER_SUPPLEMENT,
    SEVERITY_NORMAL, SEVERITY_SAFETY, SEVERITY_MAJOR,
)

T = lambda s: datetime.fromisoformat(s)


def build_world(store=None, clock=None):
    clock = clock or FixedClock(T("2026-03-01T08:00:00+08:00"))
    store = store or EventStore()
    svc = TextbookService(store, clock)
    svc.register_regulation("law-edu-2026", "教材管理条例", "第12条",
                            "2026-01-01T00:00:00+08:00")
    svc.register_regulation("law-safe-2026", "校园安全内容规范", "第7条",
                            "2026-01-01T00:00:00+08:00")
    # 第 1 版（春季）：两个单元
    svc.register_edition("edu-v1", course="数学", stage="小学三年级",
                         title="数学 三年级下册", publisher="国教社",
                         law_refs=("law-edu-2026",))
    svc.register_unit("u1-v1", "edu-v1", code="3-1", title="除法",
                      body_hash="hash-u1-v1", ordinal=1)
    svc.register_unit("u2-v1", "edu-v1", code="3-2", title="面积",
                      body_hash="hash-u2-v1", ordinal=2)
    svc.release_edition("edu-v1")
    svc.register_print_batch("edu-v1", "batch-2026-03",
                             "2026-03-05T09:00:00+08:00", quantity=5000)
    # 学校
    svc.register_school("sch-1", "第一小学")
    svc.register_school("sch-2", "第二小学")
    svc.adopt("sch-1", "edu-v1", course="数学", stage="小学三年级",
              supplement_hashes=("supp-A",), form="electronic")
    svc.adopt("sch-2", "edu-v1", course="数学", stage="小学三年级",
              supplement_hashes=("supp-B",), form="print")
    # 建链完成后把时钟推进到教学日之后，供勘误发现时间使用
    clock.set(T("2026-03-15T08:00:00+08:00"))
    return clock, store, svc


class HistoricalResolutionTest(unittest.TestCase):
    def test_distinguish_print_electronic_and_prep(self):
        clock, store, svc = build_world()
        clock.set(T("2026-03-10T08:00:00+08:00"))
        view = svc.resolve_text("sch-1", "数学", "3-1",
                                "2026-03-10T10:00:00+08:00")
        self.assertEqual(view["status"], "active")
        self.assertEqual(view["edition_id"], "edu-v1")
        self.assertEqual(view["form"], "electronic")  # 已采用电子版
        self.assertEqual(view["body_hash"], "hash-u1-v1")
        self.assertEqual(view["print_batches"],
                         {"batch-2026-03": "2026-03-05T09:00:00+08:00"})  # 在印批次可区分
        self.assertEqual(view["law_refs"][0]["article"], "第12条")
        # 印刷版学校
        view2 = svc.resolve_text("sch-2", "数学", "3-1",
                                 "2026-03-10T10:00:00+08:00")
        self.assertEqual(view2["form"], "print")

    def test_reconstruct_text_at_any_historical_date(self):
        clock, store, svc = build_world()
        # 采用之前 → 尚未采用
        early = svc.resolve_text("sch-1", "数学", "3-1",
                                 "2026-02-01T00:00:00+08:00")
        self.assertEqual(early["status"], "not_adopted_yet")
        # v1 期间
        at_v1 = svc.resolve_text("sch-1", "数学", "3-2",
                                 "2026-03-12T00:00:00+08:00")
        self.assertEqual(at_v1["body_hash"], "hash-u2-v1")
        # 出 v2（普通勘误进入下一可用版次）
        clock.set(T("2026-04-01T08:00:00+08:00"))
        svc.report_issue("n-normal", "u2-v1", "edu-v1", severity=SEVERITY_NORMAL,
                         description="面积图示标注错误",
                         found_at="2026-03-20T09:00:00+08:00")
        svc.decide_correction("n-normal", law_refs=("law-edu-2026",))
        svc.register_edition("edu-v2", course="数学", stage="小学三年级",
                             title="数学 三年级下册（修订）", publisher="国教社",
                             law_refs=("law-edu-2026",))
        svc.register_unit("u2-v2", "edu-v2", code="3-2", title="面积",
                          body_hash="hash-u2-v2", ordinal=2,
                          notice_ids=("n-normal",))
        svc.register_unit("u1-v2", "edu-v2", code="3-1", title="除法",
                          body_hash="hash-u1-v1", ordinal=1)
        svc.release_edition("edu-v2", includes_notice_ids=("n-normal",))
        svc.confirm_switch("sch-1", "数学", "edu-v2",
                           checked_course="数学", checked_stage="小学三年级",
                           checked_supplement_hashes=("supp-A",),
                           at="2026-05-01T08:00:00+08:00")
        # 历史日期仍还原 v1 文本
        hist = svc.resolve_text("sch-1", "数学", "3-2",
                                "2026-04-20T00:00:00+08:00")
        self.assertEqual(hist["edition_id"], "edu-v1")
        self.assertEqual(hist["body_hash"], "hash-u2-v1")
        # 切换后是 v2 文本
        now = svc.resolve_text("sch-1", "数学", "3-2",
                               "2026-05-02T00:00:00+08:00")
        self.assertEqual(now["edition_id"], "edu-v2")
        self.assertEqual(now["body_hash"], "hash-u2-v2")


class FreezeAndLayeredAckTest(unittest.TestCase):
    def test_safety_correction_freezes_and_layered_release(self):
        clock, store, svc = build_world()
        clock.set(T("2026-03-15T08:00:00+08:00"))
        svc.report_issue("n-safe", "u1-v1", "edu-v1", severity=SEVERITY_SAFETY,
                         description="安全提示缺失",
                         found_at="2026-03-14T10:00:00+08:00")
        svc.decide_correction("n-safe", law_refs=("law-safe-2026",),
                              replacement_edition_id=None,
                              deadline="2026-03-25T18:00:00+08:00")
        # 冻结后、确认前：文本不可用，显示冻结与待确认层
        view = svc.resolve_text("sch-1", "数学", "3-1",
                                "2026-03-16T09:00:00+08:00")
        self.assertEqual(view["status"], "frozen")
        self.assertEqual(view["confirm_progress"], [])

        # 未完成三层确认前不允许切换
        svc.register_edition("edu-v11", course="数学", stage="小学三年级",
                             title="数学 三年级下册（安全修订）", publisher="国教社",
                             law_refs=("law-safe-2026",))
        svc.register_unit("u1-v11", "edu-v11", code="3-1", title="除法",
                          body_hash="hash-u1-safe", ordinal=1)
        svc.release_edition("edu-v11")
        with self.assertRaises(BusinessError):
            svc.confirm_switch("sch-1", "数学", "edu-v11",
                               checked_course="数学", checked_stage="小学三年级",
                               checked_supplement_hashes=("supp-A",))

        # 分批确认：只解除相应内容（课程层）
        svc.acknowledge_freeze("n-safe", "sch-1", unit_ids=("u1-v1",),
                               layers=(LAYER_COURSE,))
        view = svc.resolve_text("sch-1", "数学", "3-1",
                                "2026-03-17T09:00:00+08:00")
        self.assertEqual(view["status"], "frozen")
        self.assertEqual(view["confirm_progress"], ["course"])
        # 另一所学校不受影响地独立受阻
        self.assertEqual(
            svc.resolve_text("sch-2", "数学", "3-1", "2026-03-17T09:00:00+08:00")
            ["status"], "frozen")

        # 补齐其余两层后解除
        svc.acknowledge_freeze("n-safe", "sch-1", unit_ids=("u1-v1",),
                               layers=(LAYER_STAGE, LAYER_SUPPLEMENT))
        view = svc.resolve_text("sch-1", "数学", "3-1",
                                "2026-03-18T09:00:00+08:00")
        self.assertEqual(view["status"], "active")
        svc.confirm_switch("sch-1", "数学", "edu-v11",
                           checked_course="数学", checked_stage="小学三年级",
                           checked_supplement_hashes=("supp-A",))

    def test_safety_freeze_requires_deadline(self):
        clock, store, svc = build_world()
        svc.report_issue("n-x", "u1-v1", "edu-v1", severity=SEVERITY_MAJOR,
                         description="重大教学偏差",
                         found_at="2026-03-14T10:00:00+08:00")
        with self.assertRaises(BusinessError):
            svc.decide_correction("n-x", law_refs=("law-edu-2026",))

    def test_partial_batch_only_releases_named_units(self):
        clock, store, svc = build_world()
        svc.report_issue("n-multi", "u1-v1", "edu-v1", severity=SEVERITY_SAFETY,
                         description="两单元图示安全问题",
                         found_at="2026-03-14T10:00:00+08:00")
        svc.decide_correction("n-multi", law_refs=("law-safe-2026",),
                              affected_units=("u1-v1", "u2-v1"),
                              deadline="2026-03-30T18:00:00+08:00")
        svc.acknowledge_freeze("n-multi", "sch-1",
                               unit_ids=("u1-v1",),
                               layers=(LAYER_COURSE, LAYER_STAGE, LAYER_SUPPLEMENT))
        # u1 解除、u2 仍冻结
        self.assertEqual(
            svc.resolve_text("sch-1", "数学", "3-1", "2026-03-20T09:00:00+08:00")
            ["status"], "active")
        frozen2 = svc.resolve_text("sch-1", "数学", "3-2",
                                   "2026-03-20T09:00:00+08:00")
        self.assertEqual(frozen2["status"], "frozen")


class SwitchChecksTest(unittest.TestCase):
    def _v2_ready(self, svc):
        svc.register_edition("edu-v2", course="数学", stage="小学三年级",
                             title="数学三下（修订）", publisher="国教社",
                             law_refs=("law-edu-2026",))
        svc.register_unit("u1-v2b", "edu-v2", code="3-1", title="除法",
                          body_hash="hash-new", ordinal=1)
        svc.release_edition("edu-v2")

    def test_course_stage_supplement_must_match(self):
        clock, store, svc = build_world()
        self._v2_ready(svc)
        with self.assertRaises(BusinessError):  # 课程不符
            svc.confirm_switch("sch-1", "数学", "edu-v2",
                               checked_course="语文", checked_stage="小学三年级",
                               checked_supplement_hashes=("supp-A",))
        with self.assertRaises(BusinessError):  # 学段不符
            svc.confirm_switch("sch-1", "数学", "edu-v2",
                               checked_course="数学", checked_stage="小学四年级",
                               checked_supplement_hashes=("supp-A",))
        with self.assertRaises(BusinessError):  # 本地补充材料不符
            svc.confirm_switch("sch-1", "数学", "edu-v2",
                               checked_course="数学", checked_stage="小学三年级",
                               checked_supplement_hashes=("supp-TAMPERED",))
        ok = svc.confirm_switch("sch-1", "数学", "edu-v2",
                                checked_course="数学", checked_stage="小学三年级",
                                checked_supplement_hashes=("supp-A",))
        self.assertEqual(ok.event_type, "ADOPTION_SWITCHED")


class IdempotentReconfirmTest(unittest.TestCase):
    def test_same_notice_same_content_returns_original(self):
        clock, store, svc = build_world()
        svc.report_issue("n-n", "u2-v1", "edu-v1", severity=SEVERITY_NORMAL,
                         description="普通错字",
                         found_at="2026-03-14T10:00:00+08:00")
        first = svc.decide_correction("n-n", law_refs=("law-edu-2026",))
        again = svc.decide_correction("n-n", law_refs=("law-edu-2026",))
        self.assertIs(first, again)  # 返回原结果事件
        self.assertEqual(len(store.by_type("CORRECTION_DECIDED")), 1)
        # 待核队列里没有记录
        self.assertEqual(len(store.by_type("DECISION_REVIEW_PENDING")), 0)

    def test_changed_content_is_held_for_review(self):
        clock, store, svc = build_world()
        svc.report_issue("n-c", "u1-v1", "edu-v1", severity=SEVERITY_SAFETY,
                         description="安全内容问题",
                         found_at="2026-03-14T10:00:00+08:00")
        svc.decide_correction("n-c", law_refs=("law-safe-2026",),
                              deadline="2026-03-25T18:00:00+08:00")
        # 内容变化（改了截止点与法规依据）→ 抛错并保留待核，原冻结继续有效
        with self.assertRaises(BusinessError):
            svc.decide_correction("n-c", law_refs=("law-edu-2026",),
                                  deadline="2026-04-10T18:00:00+08:00")
        pending = store.by_type("DECISION_REVIEW_PENDING")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].payload["proposal"]["deadline"],
                         "2026-04-10T18:00:00+08:00")
        # 原截止点不变，内容仍冻结
        view = svc.resolve_text("sch-1", "数学", "3-1",
                                "2026-03-20T09:00:00+08:00")
        self.assertEqual(view["status"], "frozen")
        self.assertEqual(view["deadline"], "2026-03-25T18:00:00+08:00")
        # 监管视图能看到待核记录
        trace = svc.regulatory_trace("n-c")
        self.assertEqual(len(trace["review_pending"]), 1)


class TeachingFactImmutabilityTest(unittest.TestCase):
    def test_teaching_and_prep_facts_survive_later_actions(self):
        clock, store, svc = build_world()
        svc.record_teaching("teach-1", "sch-1", "u1-v1", "edu-v1",
                            "2026-03-12T10:00:00+08:00", "hash-u1-v1")
        svc.record_prep_reference("sch-1", "u1-v1", "edu-v1",
                                  "2026-03-11T20:00:00+08:00", "hash-u1-v1")
        # 之后发生冻结、确认、切换、新版
        svc.report_issue("n-s", "u1-v1", "edu-v1", severity=SEVERITY_SAFETY,
                         description="安全提示缺失",
                         found_at="2026-03-13T08:00:00+08:00")
        svc.decide_correction("n-s", law_refs=("law-safe-2026",),
                              deadline="2026-03-30T18:00:00+08:00")
        svc.acknowledge_freeze("n-s", "sch-1", unit_ids=("u1-v1",),
                               layers=(LAYER_COURSE, LAYER_STAGE, LAYER_SUPPLEMENT))
        svc.register_edition("edu-v9", course="数学", stage="小学三年级",
                             title="数学三下（安全版）", publisher="国教社")
        svc.register_unit("u1-v9", "edu-v9", code="3-1", title="除法",
                          body_hash="hash-u1-fixed", ordinal=1)
        svc.release_edition("edu-v9")
        svc.confirm_switch("sch-1", "数学", "edu-v9",
                           checked_course="数学", checked_stage="小学三年级",
                           checked_supplement_hashes=("supp-A",))
        # 教学事实仍指向当时实际讲授的文本，不可改写
        facts = svc.teaching_facts("sch-1", "数学")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["edition_id"], "edu-v1")
        self.assertEqual(facts[0]["content_hash"], "hash-u1-v1")
        self.assertEqual(facts[0]["taught_at"], T("2026-03-12T10:00:00+08:00"))
        prep = store.by_type("PREP_REFERENCED")
        self.assertEqual(len(prep), 1)
        self.assertEqual(prep[0].payload["content_hash"], "hash-u1-v1")
        # 事件日志中不存在“删除/修改”教学事实的事件类型
        for e in store.all():
            self.assertNotIn("DELETE", e.event_type)
            self.assertNotIn("REVOKE", e.event_type)


class LateIssueTest(unittest.TestCase):
    def test_late_issue_chains_at_actual_found_time(self):
        clock, store, svc = build_world()
        # 4 月才报告，但实际发现时间是 3 月 10 日（迟到入链）
        clock.set(T("2026-04-05T08:00:00+08:00"))
        svc.report_issue("n-late", "u1-v1", "edu-v1", severity=SEVERITY_NORMAL,
                         description="迟报的排版问题",
                         found_at="2026-03-10T08:00:00+08:00")
        issue = store.by_type("ISSUE_REPORTED")[0]
        # 事件按实际发现时间入链，登记时刻单独保存在 recorded_at
        self.assertEqual(issue.occurred_at, T("2026-03-10T08:00:00+08:00"))
        self.assertEqual(issue.payload["found_at"], "2026-03-10T08:00:00+08:00")
        self.assertEqual(issue.payload["recorded_at"], "2026-04-05T08:00:00+08:00")
        # 发现时间不得晚于当前时钟
        with self.assertRaises(BusinessError):
            svc.report_issue("n-future", "u1-v1", "edu-v1",
                             severity=SEVERITY_NORMAL, description="未来问题",
                             found_at="2026-05-01T00:00:00+08:00")


class ReminderAndRecoveryTest(unittest.TestCase):
    def _frozen_world(self):
        clock, store, svc = build_world()
        clock.set(T("2026-03-15T08:00:00+08:00"))
        svc.report_issue("n-safe", "u1-v1", "edu-v1", severity=SEVERITY_SAFETY,
                         description="安全提示缺失",
                         found_at="2026-03-14T10:00:00+08:00")
        svc.decide_correction("n-safe", law_refs=("law-safe-2026",),
                              deadline="2026-03-25T18:00:00+08:00")
        return clock, store, svc

    def test_overdue_reminder_uses_injected_clock(self):
        clock, store, svc = self._frozen_world()
        reminders = ReminderService(store, clock)
        clock.set(T("2026-03-20T08:00:00+08:00"))
        self.assertEqual(reminders.overdue_freezes(), [])
        # sch-1 完成确认
        svc.acknowledge_freeze("n-safe", "sch-1", unit_ids=("u1-v1",),
                               layers=(LAYER_COURSE, LAYER_STAGE, LAYER_SUPPLEMENT),
                               at="2026-03-21T08:00:00+08:00")
        clock.set(T("2026-03-26T08:00:00+08:00"))
        overdue = reminders.overdue_freezes()
        # sch-1 已解除，只有 sch-2 逾期
        self.assertEqual({r["school_id"] for r in overdue}, {"sch-2"})
        self.assertGreater(overdue[0]["overdue_seconds"], 0)

    def test_upgrade_reminder_after_new_edition(self):
        clock, store, svc = build_world()
        clock.set(T("2026-03-21T08:00:00+08:00"))
        svc.report_issue("n-n", "u2-v1", "edu-v1", severity=SEVERITY_NORMAL,
                         description="错字", found_at="2026-03-20T08:00:00+08:00")
        svc.decide_correction("n-n")
        clock.set(T("2026-04-01T08:00:00+08:00"))
        svc.register_edition("edu-v2", course="数学", stage="小学三年级",
                             title="数学三下修订", publisher="国教社")
        svc.register_unit("u2-v2", "edu-v2", code="3-2", title="面积",
                          body_hash="h2", ordinal=2)
        svc.release_edition("edu-v2", includes_notice_ids=("n-n",))
        reminders = ReminderService(store, clock)
        upgrades = reminders.available_upgrades("sch-1")
        self.assertEqual(len(upgrades), 1)
        self.assertEqual(upgrades[0]["available_edition_id"], "edu-v2")
        self.assertEqual(upgrades[0]["included_notice_ids"], ["n-n"])

    def test_deadline_survives_process_restart(self):
        clock, store, svc = self._frozen_world()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.json"
            live = EventStore(path)
            # 把已产生的事件搬到可持久化存储
            for e in store.chronological():
                live.append(e)
            # 进程恢复：新存储从磁盘加载，截止点仍为原决定中的 3-25
            clock2 = FixedClock(T("2026-03-26T08:00:00+08:00"))
            svc2 = TextbookService(live, clock2)
            reminders2 = ReminderService(live, clock2)
            overdue = reminders2.overdue_freezes()
            schools = {r["school_id"] for r in overdue}
            self.assertEqual(schools, {"sch-1", "sch-2"})
            self.assertTrue(all(r["deadline"] == "2026-03-25T18:00:00+08:00"
                                for r in overdue))
            # 恢复后业务可继续：分层确认与历史还原正常
            svc2.acknowledge_freeze("n-safe", "sch-1", unit_ids=("u1-v1",),
                                    layers=tuple(("course", "stage", "supplement")))
            view = svc2.resolve_text("sch-1", "数学", "3-1",
                                     "2026-03-26T09:00:00+08:00")
            self.assertEqual(view["status"], "active")


class EventStoreRulesTest(unittest.TestCase):
    def test_version_must_be_continuous_and_ids_unique(self):
        store = EventStore()
        from src.event_store import Event
        e1 = Event("e1", "EDITION_REGISTERED", "textbook_edition", "ed-x",
                   T("2026-01-01T00:00:00+08:00"), 1, "x", {})
        store.append(e1)
        with self.assertRaises(Exception):
            store.append(Event("e1", "X", "textbook_edition", "ed-x",
                               T("2026-01-02T00:00:00+08:00"), 2, "x", {}))
        with self.assertRaises(Exception):  # 跳号
            store.append(Event("e2", "X", "textbook_edition", "ed-x",
                               T("2026-01-02T00:00:00+08:00"), 3, "x", {}))


class RegulatoryTraceTest(unittest.TestCase):
    def test_trace_from_replacement_record(self):
        clock, store, svc = build_world()
        svc.report_issue("n-safe", "u1-v1", "edu-v1", severity=SEVERITY_SAFETY,
                         description="安全提示缺失",
                         found_at="2026-03-14T10:00:00+08:00")
        svc.decide_correction("n-safe", law_refs=("law-safe-2026",),
                              replacement_edition_id="edu-v11",
                              replacement_unit_id="u1-v11",
                              deadline="2026-03-25T18:00:00+08:00")
        # sch-1 确认到课程层；sch-2 完全未确认
        svc.acknowledge_freeze("n-safe", "sch-1", unit_ids=("u1-v1",),
                               layers=(LAYER_COURSE,))
        trace = svc.regulatory_trace("n-safe")
        by_school = {b["school_id"]: b for b in trace["blocked_schools"]}
        self.assertTrue(by_school["sch-1"]["still_blocked"])
        self.assertEqual(by_school["sch-1"]["confirmed_layers_by_unit"]["u1-v1"],
                         ["course"])
        self.assertTrue(by_school["sch-2"]["still_blocked"])
        self.assertEqual(by_school["sch-2"]["unreleased_units"], ["u1-v1"])
        # 生效选择依据的法规
        self.assertEqual(trace["law_basis"][0]["regulation_id"], "law-safe-2026")
        self.assertEqual(trace["law_basis"][0]["article"], "第7条")
        # 勘误版本（事件链版本）
        types = [(e["event_type"], e["version"]) for e in trace["event_chain"]]
        self.assertEqual(types, [("ISSUE_REPORTED", 1), ("FREEZE_IMPOSED", 2)])
        self.assertEqual(trace["replacement"]["edition_id"], "edu-v11")
        self.assertEqual(trace["decision_event_id"], "n-safe-v2")

        # sch-1 补齐确认后不再受阻，sch-2 仍受阻
        svc.acknowledge_freeze("n-safe", "sch-1", unit_ids=("u1-v1",),
                               layers=(LAYER_STAGE, LAYER_SUPPLEMENT))
        trace2 = svc.regulatory_trace("n-safe")
        by2 = {b["school_id"]: b for b in trace2["blocked_schools"]}
        self.assertFalse(by2["sch-1"]["still_blocked"])
        self.assertTrue(by2["sch-2"]["still_blocked"])


if __name__ == "__main__":
    unittest.main()
