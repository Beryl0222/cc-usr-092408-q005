"""测试共享构造：搭建一本教材、两个版次、三类载体与若干学校。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.clock import FixedClock
from src.model import (
    CHANNEL_ELECTRONIC,
    CHANNEL_LESSON_PREP,
    CHANNEL_PRINT_BATCH,
)
from src.service import TextbookLifecycleService
from src.store import EventStore

CST = timezone(timedelta(hours=8))


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


class World:
    BOOK = "book-history-7"
    EDITION_2025 = "edition-history-7-2025"
    EDITION_2026 = "edition-history-7-2026"
    UNIT_A = "unit-7a-opium-war"
    UNIT_B = "unit-7b-westernization"
    LEGAL = "law-textbook-mgmt-2025"

    def __init__(self, start: str = "2025-08-01T09:00:00+08:00") -> None:
        self.clock = FixedClock(dt(start))
        self.store = EventStore()
        self.svc = TextbookLifecycleService(self.store, self.clock)

    def seed(self) -> "World":
        svc = self.svc
        svc.register_legal_basis(
            self.LEGAL, code="教材管理办法", version="2025-1",
            title="中小学教材管理办法", effective_from="2025-01-01",
            citation="教材管理办法 第十二条",
        )
        svc.release_edition(
            self.EDITION_2025, book_id=self.BOOK, edition_code="2025秋",
            title="中国历史 七年级上册",
        )
        for unit_id, code, title in (
            (self.UNIT_A, "7A", "鸦片战争"),
            (self.UNIT_B, "7B", "洋务运动"),
        ):
            for channel, ref, hsh in (
                (CHANNEL_PRINT_BATCH, f"{unit_id}#p2025", f"hash-{unit_id}-p2025"),
                (CHANNEL_ELECTRONIC, f"{unit_id}#e2025", f"hash-{unit_id}-e2025"),
                (CHANNEL_LESSON_PREP, f"{unit_id}#t2025", f"hash-{unit_id}-t2025"),
            ):
                svc.register_content(
                    unit_id, edition_id=self.EDITION_2025, channel=channel,
                    content_ref=ref, content_hash=hsh, unit_code=code, title=title,
                    course="history", stage="junior_1",
                )
        svc.record_adoption(
            "adoption-school-a", school_id="school-a", school_name="甲县第一中学",
            course="history", stage="junior_1", edition_id=self.EDITION_2025,
            used_unit_ids=[self.UNIT_A, self.UNIT_B],
            local_supplement_id="supp-a-v1", local_supplement_hash="supp-a-h1",
        )
        svc.record_adoption(
            "adoption-school-b", school_id="school-b", school_name="乙县实验学校",
            course="history", stage="junior_1", edition_id=self.EDITION_2025,
            used_unit_ids=[self.UNIT_A, self.UNIT_B],
            local_supplement_id="supp-b-v1", local_supplement_hash="supp-b-h1",
        )
        return self

    # ---- 便捷推进 ----
    def at(self, text: str) -> "World":
        self.clock.set(dt(text))
        return self

    def advance(self, **kwargs) -> "World":
        self.clock.advance(timedelta(**kwargs))
        return self
