"""监管追溯：从一条替换记录看到受阻学校、确认层级、法规与勘误版本。"""

import unittest

from src.model import (
    CHANNEL_PRINT_BATCH,
    LAYER_COURSE,
    LAYER_LOCAL_SUPPLEMENT,
    LAYER_STAGE,
    SEVERITY_MAJOR,
)
from tests.world import World, dt


class CorrectionTraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World().seed().at("2025-09-10T09:00:00+08:00")
        self.svc = self.world.svc
        self.svc.report_issue(
            "issue-map-1", unit_id=World.UNIT_A, channel=CHANNEL_PRINT_BATCH,
            edition_id=World.EDITION_2025, detail="地图国界绘制错误",
            discovered_at=dt("2025-09-08T10:00:00+08:00"),
            suspected_severity=SEVERITY_MAJOR,
        )
        self.svc.approve_correction(
            "corr-trace-1", severity=SEVERITY_MAJOR,
            unit_ids=[World.UNIT_A, World.UNIT_B], channels=[CHANNEL_PRINT_BATCH],
            replacement={CHANNEL_PRINT_BATCH: {"content_ref": "fix#map",
                                               "content_hash": "hash-fix-map"}},
            legal_basis_ids=[World.LEGAL], issue_ids=["issue-map-1"],
            ack_deadline=dt("2025-09-24T09:00:00+08:00"),
        )

    def test_trace_shows_blocked_schools_and_layers(self) -> None:
        trace = self.svc.trace_correction("corr-trace-1")
        self.assertTrue(trace["found"])
        self.assertEqual(trace["severity"], SEVERITY_MAJOR)
        self.assertEqual(set(trace["blocked_school_ids"]), {"school-a", "school-b"})

        school_a = next(s for s in trace["schools"] if s["school_id"] == "school-a")
        self.assertTrue(school_a["still_blocked"])
        self.assertEqual(school_a["current_edition_id"], World.EDITION_2025)
        unit_a = next(u for u in school_a["units"] if u["unit_id"] == World.UNIT_A)
        self.assertEqual(unit_a["confirmed_layers"], [])
        self.assertEqual(set(unit_a["missing_layers"]),
                         {LAYER_COURSE, LAYER_STAGE, LAYER_LOCAL_SUPPLEMENT})

        # 法规依据可追溯
        self.assertEqual(trace["legal_basis"][0]["legal_basis_id"], World.LEGAL)
        self.assertEqual(trace["legal_basis"][0]["code"], "教材管理办法")
        self.assertEqual(trace["issue_ids"], ["issue-map-1"])

    def test_trace_after_partial_and_full_confirmation(self) -> None:
        svc = self.svc
        # school-a：只确认两层，仍受阻
        svc.confirm_notice("corr-trace-1", school_id="school-a", layer=LAYER_COURSE,
                           unit_ids=[World.UNIT_A])
        svc.confirm_notice("corr-trace-1", school_id="school-a", layer=LAYER_STAGE,
                           unit_ids=[World.UNIT_A])
        # school-b：三层齐备，全部解除
        for layer in (LAYER_COURSE, LAYER_STAGE, LAYER_LOCAL_SUPPLEMENT):
            svc.confirm_notice("corr-trace-1", school_id="school-b", layer=layer)

        trace = svc.trace_correction("corr-trace-1")
        by_school = {s["school_id"]: s for s in trace["schools"]}
        self.assertTrue(by_school["school-a"]["still_blocked"])
        self.assertFalse(by_school["school-b"]["still_blocked"])
        unit_a_a = next(u for u in by_school["school-a"]["units"]
                        if u["unit_id"] == World.UNIT_A)
        self.assertEqual(unit_a_a["confirmed_layers"], [LAYER_COURSE, LAYER_STAGE])
        self.assertEqual(unit_a_a["missing_layers"], [LAYER_LOCAL_SUPPLEMENT])
        for unit in by_school["school-b"]["units"]:
            self.assertTrue(unit["released"])
            self.assertEqual(unit["missing_layers"], [])
        self.assertEqual(trace["blocked_school_ids"], ["school-a"])

    def test_trace_includes_effective_edition_and_basis_after_release(self) -> None:
        svc = self.svc
        # school-b 完成确认：替换内容挂到其当前版次（本次生效选择）
        for layer in (LAYER_COURSE, LAYER_STAGE, LAYER_LOCAL_SUPPLEMENT):
            svc.confirm_notice("corr-trace-1", school_id="school-b", layer=layer)
        trace = svc.trace_correction("corr-trace-1")
        self.assertEqual(trace["effective_edition_ids"], [World.EDITION_2025])
        school_b = next(s for s in trace["schools"] if s["school_id"] == "school-b")
        self.assertFalse(school_b["still_blocked"])

    def test_trace_unknown_correction(self) -> None:
        trace = self.svc.trace_correction("nope")
        self.assertFalse(trace["found"])


if __name__ == "__main__":
    unittest.main()
