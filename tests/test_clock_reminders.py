"""可注入时钟、到期/升级提醒与进程恢复后沿用原截止点。"""

import tempfile
import unittest
from pathlib import Path

from src.clock import FixedClock, SystemClock
from src.model import (
    CHANNEL_PRINT_BATCH,
    REMINDER_DUE,
    REMINDER_ESCALATED,
    REMINDER_OVERDUE,
    SEVERITY_MAJOR,
)
from src.service import TextbookLifecycleService
from src.store import EventStore
from tests.world import World, dt

DEADLINE = dt("2025-09-24T09:00:00+08:00")
ESCALATE = dt("2025-10-01T09:00:00+08:00")  # deadline + 7 天


class ReminderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World().seed().at("2025-09-10T09:00:00+08:00")
        self.svc = self.world.svc
        self.svc.approve_correction(
            "corr-clock-1", severity=SEVERITY_MAJOR,
            unit_ids=[World.UNIT_A], channels=[CHANNEL_PRINT_BATCH],
            replacement={CHANNEL_PRINT_BATCH: {"content_ref": "7a#fix", "content_hash": "7a-fix"}},
            legal_basis_ids=[World.LEGAL],
            ack_deadline=DEADLINE,
        )

    def test_no_reminder_before_deadline(self) -> None:
        self.world.at("2025-09-23T08:59:59+08:00")
        self.assertEqual(self.svc.due_reminders(), [])

    def test_due_at_exactly_deadline_then_overdue_then_escalation(self) -> None:
        w = self.world

        w.at(DEADLINE.isoformat())
        due = self.svc.dispatch_due_reminders()
        self.assertEqual([d["level"] for d in due], [REMINDER_DUE, REMINDER_DUE])
        self.assertEqual({d["school_id"] for d in due}, {"school-a", "school-b"})

        # 同一时刻重复派发：不再产生提醒
        self.assertEqual(self.svc.dispatch_due_reminders(), [])

        w.at("2025-09-25T09:00:00+08:00")
        overdue = self.svc.dispatch_due_reminders()
        self.assertEqual({d["level"] for d in overdue}, {REMINDER_OVERDUE})

        w.at(ESCALATE.isoformat())
        escalated = self.svc.dispatch_due_reminders()
        self.assertEqual({d["level"] for d in escalated}, {REMINDER_ESCALATED})
        # 截止点来自调度事件，与当前系统时钟无关
        for item in escalated:
            self.assertEqual(item["due_at"], DEADLINE)
            self.assertEqual(item["escalate_at"], ESCALATE)

    def test_process_recovery_keeps_original_deadline(self) -> None:
        # 进程在截止日前导出事件
        self.world.at("2025-09-12T09:00:00+08:00")
        data = self.world.store.to_json()

        # 新进程在截止点之后很久才恢复，并换一个系统时钟
        recovered_clock = FixedClock(dt("2025-10-05T12:00:00+08:00"))
        recovered_store = EventStore.load(__import__("json").loads(data))
        recovered = TextbookLifecycleService(recovered_store, recovered_clock)
        pending = recovered.dispatch_due_reminders()
        levels = sorted(d["level"] for d in pending)
        # 逾期与升级一次性补发，到期点仍是原截止点
        self.assertEqual(levels, [REMINDER_ESCALATED, REMINDER_ESCALATED,
                                  REMINDER_OVERDUE, REMINDER_OVERDUE])
        for d in pending:
            self.assertEqual(d["due_at"], DEADLINE)

        # 再恢复一次：已派发的层级不重复
        data2 = recovered_store.to_json()
        store2 = EventStore.load(__import__("json").loads(data2))
        svc2 = TextbookLifecycleService(store2, FixedClock(dt("2025-11-01T00:00:00+08:00")))
        self.assertEqual(svc2.dispatch_due_reminders(), [])

    def test_recovery_from_file_preserves_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            self.world.store.save(path)
            store = EventStore.load(path)
            self.assertEqual(
                [e["event_id"] for e in store.stream()],
                [e["event_id"] for e in self.world.store.stream()],
            )

    def test_system_clock_is_tz_aware(self) -> None:
        self.assertIsNotNone(SystemClock().now().tzinfo)


if __name__ == "__main__":
    unittest.main()
