"""勘误分级处置：普通勘误入下一版次，重大勘误先冻结；迟到问题按发现时间入链。"""

import unittest

from src.model import (
    CHANNEL_ELECTRONIC,
    CHANNEL_PRINT_BATCH,
    ROUTE_FREEZE,
    ROUTE_NEXT_EDITION,
    SEVERITY_MAJOR,
    SEVERITY_NORMAL,
)
from tests.world import World, dt


class CorrectionRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World().seed()
        self.svc = self.world.svc

    def test_normal_correction_waits_for_next_edition(self) -> None:
        w = self.world.at("2025-11-01T09:00:00+08:00")
        self.svc.approve_correction(
            "corr-normal-1", severity=SEVERITY_NORMAL,
            unit_ids=[World.UNIT_B], channels=[CHANNEL_PRINT_BATCH],
            replacement={CHANNEL_PRINT_BATCH: {"content_ref": "7b#p-fix", "content_hash": "7b-p-fix"}},
            legal_basis_ids=[World.LEGAL],
        )
        # 旧版次文本不回贴：冻结期内仍读旧文本
        old = self.svc.text_at("school-a", World.UNIT_B, CHANNEL_PRINT_BATCH,
                               dt("2025-12-01T08:00:00+08:00"))
        self.assertEqual(old["content_hash"], f"hash-{World.UNIT_B}-p2025")

        w.at("2026-06-01T09:00:00+08:00")
        self.svc.release_edition(
            World.EDITION_2026, book_id=World.BOOK, edition_code="2026秋",
            supersedes=World.EDITION_2025,
        )
        state = self.svc.state()
        corr = state.corrections["corr-normal-1"]
        self.assertEqual(corr["route"], ROUTE_NEXT_EDITION)
        # 替换内容自动进入下个可用版次并标注来源勘误
        binding = state.binding_at(World.UNIT_B, World.EDITION_2026,
                                   CHANNEL_PRINT_BATCH, dt("2026-06-02T00:00:00+08:00"))
        self.assertIsNotNone(binding)
        self.assertEqual(binding.content_hash, "7b-p-fix")
        self.assertEqual(binding.source_correction_id, "corr-normal-1")

    def test_major_correction_freezes_and_blocks_switch(self) -> None:
        w = self.world.at("2025-09-10T09:00:00+08:00")
        self.svc.approve_correction(
            "corr-major-1", severity=SEVERITY_MAJOR,
            unit_ids=[World.UNIT_A], channels=[CHANNEL_PRINT_BATCH, CHANNEL_ELECTRONIC],
            replacement={
                CHANNEL_PRINT_BATCH: {"content_ref": "7a#p-fix", "content_hash": "7a-p-fix"},
                CHANNEL_ELECTRONIC: {"content_ref": "7a#e-fix", "content_hash": "7a-e-fix"},
            },
            legal_basis_ids=[World.LEGAL],
            ack_deadline=dt("2025-09-24T09:00:00+08:00"),
        )
        state = self.svc.state()
        self.assertEqual(state.corrections["corr-major-1"]["route"], ROUTE_FREEZE)
        # 两所采用学校都被冻结
        self.assertEqual({f["school_id"] for f in state.freezes}, {"school-a", "school-b"})

        # 未完成确认不能切换版次
        w.at("2026-06-01T09:00:00+08:00")
        self.svc.release_edition(
            World.EDITION_2026, book_id=World.BOOK, edition_code="2026秋",
            supersedes=World.EDITION_2025,
        )
        self.svc.register_content(
            World.UNIT_A, edition_id=World.EDITION_2026, channel=CHANNEL_PRINT_BATCH,
            content_ref="x", content_hash="h2026", unit_code="7A",
            course="history", stage="junior_1",
        )
        with self.assertRaisesRegex(Exception, "仍处于冻结"):
            self.svc.complete_version_switch(
                "adoption-school-a", World.EDITION_2026,
                checked_course=True, checked_stage=True, checked_local_supplement=True,
            )

    def test_late_issue_chains_by_actual_discovery_time(self) -> None:
        # 2026 年才上报，但问题实际发现于 2025-09-05
        self.world.at("2026-03-01T10:00:00+08:00")
        self.svc.report_issue(
            "issue-late-1", unit_id=World.UNIT_A, channel=CHANNEL_PRINT_BATCH,
            edition_id=World.EDITION_2025, detail="地图边界标注错误",
            discovered_at=dt("2025-09-05T14:00:00+08:00"),
            suspected_severity=SEVERITY_MAJOR,
        )
        state = self.svc.state()
        issue = state.issues["issue-late-1"]
        self.assertEqual(issue["discovered_at"], dt("2025-09-05T14:00:00+08:00"))
        self.assertEqual(issue["received_at"], dt("2026-03-01T10:00:00+08:00"))
        # 重放顺序由发现时间决定，而非接收时间
        times = [e["_at"] for e in state.events if e["event_type"] in ("ISSUE_REPORTED",)]
        self.assertEqual(times, [dt("2025-09-05T14:00:00+08:00")])

    def test_new_school_adoption_is_frozen_by_existing_major_correction(self) -> None:
        w = self.world.at("2025-09-10T09:00:00+08:00")
        self.svc.approve_correction(
            "corr-major-2", severity=SEVERITY_MAJOR,
            unit_ids=[World.UNIT_A], channels=[CHANNEL_PRINT_BATCH],
            replacement={CHANNEL_PRINT_BATCH: {"content_ref": "7a#p-fix2", "content_hash": "7a-p-fix2"}},
            legal_basis_ids=[World.LEGAL],
            ack_deadline=dt("2025-09-24T09:00:00+08:00"),
        )
        w.at("2025-09-15T09:00:00+08:00")
        self.svc.record_adoption(
            "adoption-school-c", school_id="school-c", school_name="丙镇中学",
            course="history", stage="junior_1", edition_id=World.EDITION_2025,
            used_unit_ids=[World.UNIT_A, World.UNIT_B],
        )
        frozen = self.svc.text_at("school-c", World.UNIT_A, CHANNEL_PRINT_BATCH,
                                  dt("2025-09-16T08:00:00+08:00"))
        self.assertEqual(frozen["status"], "FROZEN")


class TeachingFactImmutabilityTest(unittest.TestCase):
    def test_teaching_fact_survives_later_correction(self) -> None:
        world = World().seed()
        svc = world.svc
        # 9 月 1 日按旧文本授课，留下教学事实
        taught = dt("2025-09-01T10:00:00+08:00")
        svc.record_teaching_fact(
            "fact-0001", school_id="school-a", adoption_id="adoption-school-a",
            unit_id=World.UNIT_A, channel=CHANNEL_PRINT_BATCH,
            edition_id=World.EDITION_2025, taught_at=taught,
            content_ref=f"{World.UNIT_A}#p2025",
            content_hash=f"hash-{World.UNIT_A}-p2025",
        )
        # 之后的勘误与版次切换
        world.at("2025-09-10T09:00:00+08:00")
        svc.approve_correction(
            "corr-major-x", severity=SEVERITY_MAJOR,
            unit_ids=[World.UNIT_A], channels=[CHANNEL_PRINT_BATCH],
            replacement={CHANNEL_PRINT_BATCH: {"content_ref": "7a#p-fix", "content_hash": "7a-p-fix"}},
            legal_basis_ids=[World.LEGAL],
            ack_deadline=dt("2025-09-24T09:00:00+08:00"),
        )
        state = svc.state()
        fact = next(f for f in state.teaching_facts if f["fact_id"] == "fact-0001")
        self.assertEqual(fact["content_hash"], f"hash-{World.UNIT_A}-p2025")
        self.assertEqual(fact["taught_at"], taught)
        # 事件流中不存在删除/改写教学事实的事件类型
        types = {e["event_type"] for e in world.store.stream()}
        self.assertNotIn("TEACHING_FACT_RETRACTED", types)

        # 事件存储拒绝覆盖既有事件
        from src.model import ConcurrencyError
        with self.assertRaises(ConcurrencyError):
            world.store.append(
                event_id="forged", event_type="TEACHING_FACT_RECORDED",
                aggregate_type="teaching_fact", aggregate_id="fact-0001",
                occurred_at=taught, payload={"content_hash": "rewritten"},
                expected_version=0,
            )


if __name__ == "__main__":
    unittest.main()
