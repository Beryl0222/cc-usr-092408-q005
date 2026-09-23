"""端到端：重大勘误冻结 -> 新版次编入 -> 学校确认 -> 切换 -> 文本与依据可还原。"""

import unittest

from src.model import (
    CHANNEL_PRINT_BATCH,
    LAYER_COURSE,
    LAYER_LOCAL_SUPPLEMENT,
    LAYER_STAGE,
    SEVERITY_MAJOR,
    SEVERITY_NORMAL,
)
from tests.world import World, dt


class EndToEndTest(unittest.TestCase):
    def test_freeze_confirm_switch_e2e(self) -> None:
        w = World().seed()
        svc = w.svc

        # 2025-09 重大勘误：两所学校的 UNIT_A 印刷内容冻结
        w.at("2025-09-10T09:00:00+08:00")
        svc.approve_correction(
            "corr-e2e", severity=SEVERITY_MAJOR,
            unit_ids=[World.UNIT_A], channels=[CHANNEL_PRINT_BATCH],
            replacement={CHANNEL_PRINT_BATCH: {"content_ref": "7a#p-safe",
                                               "content_hash": "hash-7a-p-safe"}},
            legal_basis_ids=[World.LEGAL],
            ack_deadline=dt("2025-09-24T09:00:00+08:00"),
        )

        # 2026-06 新版次发布：修正自动编入
        w.at("2026-06-01T09:00:00+08:00")
        svc.release_edition(
            World.EDITION_2026, book_id=World.BOOK, edition_code="2026秋",
            supersedes=World.EDITION_2025,
        )
        state = svc.state()
        binding = state.binding_at(World.UNIT_A, World.EDITION_2026,
                                   CHANNEL_PRINT_BATCH, dt("2026-06-02T00:00:00+08:00"))
        self.assertEqual(binding.content_hash, "hash-7a-p-safe")
        self.assertEqual(binding.source_correction_id, "corr-e2e")

        # 2026-08 学校先在旧版次上完成三层确认（解除冻结），再核对切换
        w.at("2026-08-20T09:00:00+08:00")
        for layer in (LAYER_COURSE, LAYER_STAGE, LAYER_LOCAL_SUPPLEMENT):
            svc.confirm_notice("corr-e2e", school_id="school-a", layer=layer)
        old_fixed = svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                                dt("2026-08-20T10:00:00+08:00"))
        self.assertEqual(old_fixed["status"], "AVAILABLE")
        self.assertEqual(old_fixed["content_hash"], "hash-7a-p-safe")
        self.assertEqual(old_fixed["legal_basis_ids"], [World.LEGAL])

        svc.request_version_switch("adoption-school-a", World.EDITION_2026)
        w.at("2026-08-25T09:00:00+08:00")
        svc.complete_version_switch(
            "adoption-school-a", World.EDITION_2026,
            checked_course=True, checked_stage=True, checked_local_supplement=True,
            local_supplement_id="supp-a-v2", local_supplement_hash="supp-a-h2",
        )
        on_new = svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                             dt("2026-08-26T08:00:00+08:00"))
        self.assertEqual(on_new["edition_id"], World.EDITION_2026)
        self.assertEqual(on_new["content_hash"], "hash-7a-p-safe")
        self.assertEqual(on_new["legal_basis_ids"], [World.LEGAL])
        self.assertEqual(on_new["correction_id"], "corr-e2e")

        # 未确认的 school-b 仍受阻，且不能切换
        blocked = svc.text_at("school-b", World.UNIT_A, CHANNEL_PRINT_BATCH,
                              dt("2026-08-26T08:00:00+08:00"))
        self.assertEqual(blocked["status"], "FROZEN")
        with self.assertRaisesRegex(Exception, "仍处于冻结"):
            svc.complete_version_switch(
                "adoption-school-b", World.EDITION_2026,
                checked_course=True, checked_stage=True, checked_local_supplement=True,
            )

        # 监管追溯一次性呈现全局
        trace = svc.trace_correction("corr-e2e")
        self.assertEqual(trace["blocked_school_ids"], ["school-b"])
        self.assertEqual(trace["effective_edition_ids"],
                         sorted([World.EDITION_2025, World.EDITION_2026]))
        self.assertEqual(trace["legal_basis"][0]["code"], "教材管理办法")

    def test_normal_correction_shows_basis_after_new_edition(self) -> None:
        w = World().seed()
        w.at("2025-11-01T09:00:00+08:00")
        w.svc.approve_correction(
            "corr-n", severity=SEVERITY_NORMAL,
            unit_ids=[World.UNIT_B], channels=[CHANNEL_PRINT_BATCH],
            replacement={CHANNEL_PRINT_BATCH: {"content_ref": "7b#p2", "content_hash": "7b-p2"}},
            legal_basis_ids=[World.LEGAL],
        )
        w.at("2026-06-01T09:00:00+08:00")
        w.svc.release_edition(
            World.EDITION_2026, book_id=World.BOOK, edition_code="2026秋",
            supersedes=World.EDITION_2025,
        )
        w.svc.request_version_switch("adoption-school-a", World.EDITION_2026)
        w.at("2026-08-25T09:00:00+08:00")
        w.svc.complete_version_switch(
            "adoption-school-a", World.EDITION_2026,
            checked_course=True, checked_stage=True, checked_local_supplement=True,
        )
        text = w.svc.text_at("school-a", World.UNIT_B, CHANNEL_PRINT_BATCH,
                             dt("2026-09-01T08:00:00+08:00"))
        self.assertEqual(text["content_hash"], "7b-p2")
        self.assertEqual(text["correction_id"], "corr-n")
        self.assertEqual(text["legal_basis_ids"], [World.LEGAL])


if __name__ == "__main__":
    unittest.main()
