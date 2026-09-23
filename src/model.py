"""领域模型：枚举、常量与异常。

事件一律使用不可变的字典信封（与 contracts/domain.schema.json 对齐），
业务状态由重放得到，不在事件之外保存可变事实。
"""

from __future__ import annotations


# ---- 聚合类型 ----
AGG_EDITION = "textbook_edition"
AGG_UNIT = AGG_CONTENT_UNIT = "content_unit"
AGG_CORRECTION = "correction_notice"
AGG_ADOPTION = "course_adoption"
AGG_LEGAL_BASIS = "legal_basis"
AGG_ISSUE = "textbook_issue"
AGG_REMINDER = "reminder"
AGG_TEACHING_FACT = "teaching_fact"

# ---- 事件名称 ----
EV_EDITION_RELEASED = "EDITION_RELEASED"
EV_UNIT_REGISTERED = "CONTENT_UNIT_REGISTERED"
EV_CONTENT_REFERENCE = "CONTENT_REFERENCE_RECORDED"
EV_LEGAL_BASIS_REGISTERED = "LEGAL_BASIS_REGISTERED"
EV_ADOPTION_RECORDED = "ADOPTION_RECORDED"
EV_ISSUE_REPORTED = "ISSUE_REPORTED"
EV_CORRECTION_APPROVED = "CORRECTION_APPROVED"
EV_ADOPTION_FROZEN = "ADOPTION_FROZEN"
EV_NOTICE_ACKNOWLEDGED = "NOTICE_ACKNOWLEDGED"
EV_SWITCH_REQUESTED = "ADOPTION_SWITCH_REQUESTED"
EV_SWITCH_COMPLETED = "ADOPTION_SWITCHED"
EV_TEACHING_FACT = "TEACHING_FACT_RECORDED"
EV_REMINDER_SCHEDULED = "REMINDER_SCHEDULED"
EV_REMINDER_DELIVERED = "REMINDER_DELIVERED"

# ---- 勘误分级与处置路径 ----
SEVERITY_NORMAL = "normal"            # 普通勘误
SEVERITY_MAJOR = "major"              # 安全性或教学影响重大
ROUTE_NEXT_EDITION = "next_edition"   # 进入下个可用版次
ROUTE_FREEZE = "freeze"               # 先冻结，等待学校确认

# ---- 内容载体（区分印刷批次 / 电子版 / 备课引用） ----
CHANNEL_PRINT_BATCH = "print_batch"
CHANNEL_ELECTRONIC = "electronic"
CHANNEL_LESSON_PREP = "lesson_prep"
CHANNELS = (CHANNEL_PRINT_BATCH, CHANNEL_ELECTRONIC, CHANNEL_LESSON_PREP)

# ---- 切换版次前需逐层核对的三层 ----
LAYER_COURSE = "course"                # 课程
LAYER_STAGE = "stage"                  # 学段
LAYER_LOCAL_SUPPLEMENT = "local_supplement"  # 本地补充材料
SWITCH_LAYERS = (LAYER_COURSE, LAYER_STAGE, LAYER_LOCAL_SUPPLEMENT)

# ---- 通知确认结果 ----
ACK_CONFIRMED = "CONFIRMED"                       # 首次确认，解除本层对应内容
ACK_DUPLICATE = "DUPLICATE"                       # 同一通知再次确认，返回原结果
ACK_HELD_CONTENT_CHANGED = "HELD_CONTENT_CHANGED"  # 内容已变化，保留待核

# ---- 提醒级别 ----
REMINDER_DUE = "due"
REMINDER_OVERDUE = "overdue"
REMINDER_ESCALATED = "escalated"

# ---- 历史文本查询状态 ----
STATUS_AVAILABLE = "AVAILABLE"
STATUS_FROZEN = "FROZEN"


class DomainError(Exception):
    """领域规则冲突。"""


class ConcurrencyError(DomainError):
    """聚合版本号冲突（已有后继事件）。"""


class UnknownReference(DomainError):
    """引用了不存在的版次、单元、法规或采用关系。"""
