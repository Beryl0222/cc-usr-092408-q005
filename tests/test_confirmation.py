"""分层分批确认、通知幂等与内容变化保留待核。"""

import unittest

from src.model import (
    ACK_CONFIRMED,
    ACK_DUPLICATE,
    ACK_HELD_CONTENT_CHANGED,
    CHANNEL_PRINT_BATCH,
    LAYER_COURSE,
    LAYER_LOCAL_SUPPLEMENT,
    LAYER_STAGE,
    SEVERITY_MAJOR,
)
from tests.world import World, dt


def approve_major(svc, correction_id="corr-major-ack", units=None, deadline="2025-09-24T09:00:00+08:00"):
    units = units or [World.UNIT_A, World.UNIT_B]
    return svc.approve_correction(
        correction_id, severity=SEVERITY_MAJOR,
        unit_ids=units, channels=[CHANNEL_PRINT_BATCH],
        replacement={CHANNEL_PRINT_BATCH: {"content_ref": f"{correction_id}#fix",
                                           "content_hash": f"hash-{correction_id}"}},
        legal_basis_ids=[World.LEGAL],
        ack_deadline=dt(deadline),
    )


class ConfirmationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World().seed().at("2025-09-10T09:00:00+08:00")
        self.svc = self.world.svc
        approve_major(self.svc)

    def test_partial_layers_do_not_release(self) -> None:
        svc = self.svc
        r1 = svc.confirm_notice("corr-major-ack", school_id="school-a", layer=LAYER_COURSE)
        self.assertEqual(r1["result"], ACK_CONFIRMED)
        self.assertEqual(r1["released_unit_ids"], [])
        frozen = svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                             dt("2025-09-10T10:00:00+08:00"))
        self.assertEqual(frozen["status"], "FROZEN")
        r2 = svc.confirm_notice("corr-major-ack", school_id="school-a", layer=LAYER_STAGE)
        self.assertEqual(r2["released_unit_ids"], [])

    def test_three_layers_release_and_bind_replacement(self) -> None:
        svc = self.svc
        # 冻结从 2025-09-10 09:00 生效；学校次日完成三层确认
        self.world.at("2025-09-11T09:00:00+08:00")
        svc.confirm_notice("corr-major-ack", school_id="school-a", layer=LAYER_COURSE)
        svc.confirm_notice("corr-major-ack", school_id="school-a", layer=LAYER_STAGE)
        r3 = svc.confirm_notice("corr-major-ack", school_id="school-a",
                                layer=LAYER_LOCAL_SUPPLEMENT)
        self.assertEqual(set(r3["released_unit_ids"]), {World.UNIT_A, World.UNIT_B})
        text = svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                           dt("2025-09-11T09:30:00+08:00"))
        self.assertEqual(text["status"], "AVAILABLE")
        self.assertEqual(text["content_hash"], "hash-corr-major-ack")
        self.assertEqual(text["correction_id"], "corr-major-ack")
        self.assertEqual(text["legal_basis_ids"], [World.LEGAL])
        # 冻结生效前的历史文本仍是旧内容
        pre = svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                          dt("2025-09-09T08:00:00+08:00"))
        self.assertEqual(pre["status"], "AVAILABLE")
        self.assertEqual(pre["content_hash"], f"hash-{World.UNIT_A}-p2025")
        # 冻结区间被完整保留：确认前的那一天仍显示 FROZEN，不可被改写
        during = svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                             dt("2025-09-10T12:00:00+08:00"))
        self.assertEqual(during["status"], "FROZEN")
        # 另一所学校仍受阻
        other = svc.text_at("school-b", World.UNIT_A, CHANNEL_PRINT_BATCH,
                            dt("2025-09-11T09:30:00+08:00"))
        self.assertEqual(other["status"], "FROZEN")

    def test_batch_only_releases_listed_units(self) -> None:
        svc = self.svc
        # 分批：先只确认 UNIT_A 三层
        for layer in (LAYER_COURSE, LAYER_STAGE, LAYER_LOCAL_SUPPLEMENT):
            result = svc.confirm_notice("corr-major-ack", school_id="school-a",
                                        layer=layer, unit_ids=[World.UNIT_A])
        a = svc.text_at("school-a", World.UNIT_A, CHANNEL_PRINT_BATCH,
                        dt("2025-09-10T10:00:00+08:00"))
        b = svc.text_at("school-a", World.UNIT_B, CHANNEL_PRINT_BATCH,
                        dt("2025-09-10T10:00:00+08:00"))
        self.assertEqual(a["status"], "AVAILABLE")
        self.assertEqual(b["status"], "FROZEN")

        # 随后扩批至全部单元确认三层：A 已解除不重复挂入，B 在三层齐备后解除
        svc.confirm_notice("corr-major-ack", school_id="school-a", layer=LAYER_COURSE)
        svc.confirm_notice("corr-major-ack", school_id="school-a",
                           layer=LAYER_STAGE)
        last = svc.confirm_notice("corr-major-ack", school_id="school-a",
                                  layer=LAYER_LOCAL_SUPPLEMENT)
        self.assertEqual(last["released_unit_ids"], [World.UNIT_B])
        b = svc.text_at("school-a", World.UNIT_B, CHANNEL_PRINT_BATCH,
                        dt("2025-09-10T10:00:00+08:00"))
        self.assertEqual(b["status"], "AVAILABLE")

        # 同一通知、同一批次再次确认 -> 返回原结果，不新增事件
        dup = svc.confirm_notice("corr-major-ack", school_id="school-a",
                                 layer=LAYER_STAGE)
        self.assertEqual(dup["result"], ACK_DUPLICATE)

    def test_duplicate_notice_returns_original_result(self) -> None:
        svc = self.svc
        first = svc.confirm_notice("corr-major-ack", school_id="school-a",
                                   layer=LAYER_COURSE, unit_ids=[World.UNIT_A])
        original_event = first["event"]["event_id"]
        again = svc.confirm_notice("corr-major-ack", school_id="school-a",
                                   layer=LAYER_COURSE, unit_ids=[World.UNIT_A])
        self.assertEqual(again["result"], ACK_DUPLICATE)
        self.assertEqual(again["original_ack_event_id"], original_event)
        self.assertIsNone(again["event"])
        # 没有新增确认事件
        state = svc.state()
        confirms = [a for a in state.acks if a["result"] == ACK_CONFIRMED]
        self.assertEqual(len(confirms), 1)

    def test_content_changed_after_first_confirm_is_held(self) -> None:
        svc = self.svc
        svc.confirm_notice("corr-major-ack", school_id="school-a",
                           layer=LAYER_COURSE, unit_ids=[World.UNIT_A])
        # 之后出现影响同一单元的新重大勘误（内容已变化）
        self.world.at("2025-09-12T09:00:00+08:00")
        svc.approve_correction(
            "corr-major-newer", severity=SEVERITY_MAJOR,
            unit_ids=[World.UNIT_A], channels=[CHANNEL_PRINT_BATCH],
            replacement={CHANNEL_PRINT_BATCH: {"content_ref": "7a#newer",
                                               "content_hash": "hash-7a-newer"}},
            legal_basis_ids=[World.LEGAL],
            ack_deadline=dt("2025-09-26T09:00:00+08:00"),
        )
        # 旧通知再次确认 -> 保留待核，不解除
        held = svc.confirm_notice("corr-major-ack", school_id="school-a",
                                  layer=LAYER_COURSE, unit_ids=[World.UNIT_A])
        self.assertEqual(held["result"], ACK_HELD_CONTENT_CHANGED)
        self.assertEqual(held["released_unit_ids"], [])
        state = svc.state()
        held_acks = [a for a in state.acks if a["result"] == ACK_HELD_CONTENT_CHANGED]
        self.assertEqual(len(held_acks), 1)
        # 原确认仍保留；新勘误独立冻结该单元
        self.assertTrue(state.active_freeze("corr-major-newer", "school-a",
                                            World.UNIT_A, CHANNEL_PRINT_BATCH,
                                            dt("2025-09-13T00:00:00+08:00")))

    def test_invalid_layer_and_batch_rejected(self) -> None:
        svc = self.svc
        with self.assertRaisesRegex(Exception, "核对层次"):
            svc.confirm_notice("corr-major-ack", school_id="school-a", layer="grade")
        with self.assertRaisesRegex(Exception, "未冻结单元"):
            svc.confirm_notice("corr-major-ack", school_id="school-a",
                               layer=LAYER_COURSE, unit_ids=["unit-not-frozen"])


if __name__ == "__main__":
    unittest.main()
