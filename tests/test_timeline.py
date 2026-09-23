"""有效区间接续：任意历史日期还原当时文本，版次切换形成左闭右开区间。"""

import unittest

from src.model import (
    CHANNEL_ELECTRONIC,
    CHANNEL_PRINT_BATCH,
    STATUS_FROZEN,
)
from tests.world import World, dt


class TimelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World().seed()
        self.svc = self.world.svc

    def test_historical_date_returns_text_as_of_then(self) -> None:
        w = self.world
        # 采用登记之前（2025-08-01 当天已登记；前一天无采用）
        before = w.svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                               dt("2025-07-31T00:00:00+08:00"))
        self.assertEqual(before["status"], "NO_ADOPTION")

        first_day = w.svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                                  dt("2025-09-01T08:00:00+08:00"))
        self.assertEqual(first_day["content_hash"], f"hash-{World.UNIT_A}-p2025")
        self.assertEqual(first_day["edition_id"], World.EDITION_2025)

    def test_channels_are_distinct(self) -> None:
        # 印刷批次、电子版、教师备课引用各自独立解析，互不混淆。
        moment = dt("2025-09-01T08:00:00+08:00")
        hashes = {
            channel: self.svc.text_at("school-a", World.UNIT_A, channel, moment)["content_hash"]
            for channel in ("print_batch", "electronic", "lesson_prep")
        }
        self.assertEqual(len(set(hashes.values())), 3)

    def test_switch_builds_half_open_intervals(self) -> None:
        w = self.world.at("2026-06-01T09:00:00+08:00")
        self.svc.release_edition(
            World.EDITION_2026, book_id=World.BOOK, edition_code="2026秋",
            title="中国历史 七年级上册（修订）", supersedes=World.EDITION_2025,
        )
        for channel, suffix in (
            (CHANNEL_PRINT_BATCH, "p2026"),
            (CHANNEL_ELECTRONIC, "e2026"),
        ):
            self.svc.register_content(
                World.UNIT_A, edition_id=World.EDITION_2026, channel=channel,
                content_ref=f"{World.UNIT_A}#{suffix[0]}2026",
                content_hash=f"hash-{World.UNIT_A}-{suffix}",
                unit_code="7A", title="鸦片战争", course="history", stage="junior_1",
            )
        self.svc.request_version_switch("adoption-school-a", World.EDITION_2026)
        switch_moment = dt("2026-08-25T10:00:00+08:00")
        w.at(switch_moment.isoformat())
        self.svc.complete_version_switch(
            "adoption-school-a", World.EDITION_2026,
            checked_course=True, checked_stage=True, checked_local_supplement=True,
            local_supplement_id="supp-a-v2", local_supplement_hash="supp-a-h2",
        )

        just_before = dt("2026-08-25T09:59:59+08:00")
        just_after = dt("2026-08-25T10:00:00+08:00")
        old = self.svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH, just_before)
        new = self.svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH, just_after)
        self.assertEqual(old["edition_id"], World.EDITION_2025)
        self.assertEqual(old["content_hash"], f"hash-{World.UNIT_A}-p2025")
        self.assertEqual(new["edition_id"], World.EDITION_2026)
        self.assertEqual(new["content_hash"], f"hash-{World.UNIT_A}-p2026")
        # 历史日期永远给旧文本
        old_again = self.svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                                     dt("2025-09-01T08:00:00+08:00"))
        self.assertEqual(old_again["content_hash"], f"hash-{World.UNIT_A}-p2025")
        # 未切换的学校保持旧版
        b = self.svc.text_at("school-b", World.UNIT_A, CHANNEL_PRINT_BATCH, just_after)
        self.assertEqual(b["edition_id"], World.EDITION_2025)

    def test_switch_requires_three_layer_checks(self) -> None:
        w = self.world.at("2026-06-01T09:00:00+08:00")
        self.svc.release_edition(
            World.EDITION_2026, book_id=World.BOOK, edition_code="2026秋",
            supersedes=World.EDITION_2025,
        )
        self.svc.register_content(
            World.UNIT_A, edition_id=World.EDITION_2026, channel=CHANNEL_PRINT_BATCH,
            content_ref="x", content_hash="h2026",
            unit_code="7A", course="history", stage="junior_1",
        )
        w.at("2026-08-25T10:00:00+08:00")
        with self.assertRaisesRegex(Exception, "层次未核对"):
            self.svc.complete_version_switch(
                "adoption-school-a", World.EDITION_2026,
                checked_course=True, checked_stage=True, checked_local_supplement=False,
            )

    def test_frozen_content_has_no_text_until_released(self) -> None:
        w = self.world.at("2025-09-10T09:00:00+08:00")
        self.svc.approve_correction(
            "corr-major-1", severity="major",
            unit_ids=[World.UNIT_A], channels=[CHANNEL_PRINT_BATCH, CHANNEL_ELECTRONIC],
            replacement={
                CHANNEL_PRINT_BATCH: {"content_ref": "unit-7a#p-fixed", "content_hash": "hash-7a-p-fixed"},
                CHANNEL_ELECTRONIC: {"content_ref": "unit-7a#e-fixed", "content_hash": "hash-7a-e-fixed"},
            },
            legal_basis_ids=[World.LEGAL],
            ack_deadline=dt("2025-09-24T09:00:00+08:00"),
        )
        frozen = self.svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                                  dt("2025-09-11T08:00:00+08:00"))
        self.assertEqual(frozen["status"], STATUS_FROZEN)
        self.assertIsNone(frozen["content_ref"])
        self.assertEqual(frozen["frozen_by"], "corr-major-1")
        # 冻结开始前的历史文本仍可还原
        pre = self.svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                               dt("2025-09-09T08:00:00+08:00"))
        self.assertEqual(pre["content_hash"], "hash-unit-7a-opium-war-p2025")
        # 未受影响的载体照常可读
        prep = self.svc.text_at("school-a", World.UNIT_A, "lesson_prep",
                                dt("2025-09-11T08:00:00+08:00"))
        self.assertEqual(prep["status"], "AVAILABLE")


if __name__ == "__main__":
    unittest.main()
