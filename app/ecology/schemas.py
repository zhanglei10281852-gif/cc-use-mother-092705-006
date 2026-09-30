from __future__ import annotations

from pydantic import BaseModel, Field


class PileType:
    LEAF = "落叶"
    BRANCH = "枝堆"
    MIXED = "混合堆积"
    OTHER = "其他"
    ALL = (LEAF, BRANCH, MIXED, OTHER)


class RiskLevel:
    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"
    ALL = (LOW, MEDIUM, HIGH)


class DecisionValue:
    KEEP = "保留"
    ADJUST = "调整"
    REMOVE = "移除"
    ALL = (KEEP, ADJUST, REMOVE)


class ReviewConclusion:
    KEEP = "维持保留"
    ADJUST = "建议调整"
    REMOVE = "建议移除"
    OBSERVE = "继续观察"
    ALL = (KEEP, ADJUST, REMOVE, OBSERVE)


class RegistrationCreate(BaseModel):
    """工作人员对落叶/枝堆现场的初次登记。"""

    site_name: str = Field(..., min_length=1, max_length=100, description="堆放地点名称，如 滨河公园北门")
    location_text: str = Field(..., min_length=1, max_length=200, description="具体堆放位置描述")
    pile_type: str = Field(..., description="堆积类型：落叶/枝堆/混合堆积/其他")
    photo_summary: str = Field("", max_length=1000, description="现场照片摘要（非图片本身）")
    season_risk: str = Field("", max_length=1000, description="季节风险描述，如越冬期栖息地")
    risk_level: str = Field(RiskLevel.MEDIUM, description="风险等级：低/中/高")
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    happened_at: str | None = Field(None, description="现场发现时间（ISO8601），缺省为当前时间")
    note: str = Field("", max_length=1000)


class DecisionCreate(BaseModel):
    """授权人员出具保留/调整/移除决定，必须引用当时的生态依据。"""

    decision: str = Field(..., description="决定：保留/调整/移除")
    ecological_basis: str = Field(..., min_length=1, max_length=2000, description="作出决定时引用的生态依据")
    basis_refs: list[str] = Field(default_factory=list, description="依据出处，如导则名称、条款、观察记录编号")
    measures: str = Field("", max_length=1000, description="调整措施（decision=调整 时必填具体做法）")
    review_due_at: str | None = Field(None, description="要求下次复查期限")


class ReviewCreate(BaseModel):
    """复查结论，只能新增，不允许覆盖或修改历史结论。"""

    conclusion_type: str = Field(..., description="复查结论：维持保留/建议调整/建议移除/继续观察")
    finding: str = Field(..., min_length=1, max_length=2000, description="复查发现")
    photo_summary: str = Field("", max_length=1000)
    next_review_at: str | None = None
    reviewed_at: str | None = None


class EmergencyCreate(BaseModel):
    """紧急安全处置：先执行，后补录，必须标明补录期限与责任人。"""

    site_name: str = Field(..., min_length=1, max_length=100)
    location_text: str = Field(..., min_length=1, max_length=200)
    pile_type: str = Field(PileType.OTHER)
    action_taken: str = Field(..., min_length=1, max_length=300, description="已采取的紧急处置措施")
    reason: str = Field(..., min_length=1, max_length=500, description="紧急处置原因，如阻断道路、火情风险")
    hazard_type: str = Field("", max_length=100, description="险情类型")
    happened_at: str | None = Field(None, description="实际处置时间，可早于登记时间")
    supplement_deadline: str | None = Field(None, description="补录期限，缺省为处置后 48 小时")
    responsible_person: str = Field(..., min_length=1, max_length=50, description="补录责任人")
    photo_summary: str = Field("", max_length=1000)
    note: str = Field("", max_length=1000)


class SupplementCreate(BaseModel):
    """紧急处置后的补录信息。"""

    pile_type: str = Field(..., description="堆积类型：落叶/枝堆/混合堆积/其他")
    photo_summary: str = Field("", max_length=1000)
    season_risk: str = Field("", max_length=1000)
    risk_level: str = Field(RiskLevel.MEDIUM)
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    happened_at: str | None = Field(None, description="现场发现时间，缺省取紧急处置时间")
    note: str = Field("", max_length=1000)


class ComplaintCreate(BaseModel):
    """公众投诉入口（无需登录），登记为反馈并合并到同地点同周期案件。"""

    site_name: str = Field(..., min_length=1, max_length=100)
    location_text: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=1000, description="投诉内容")
    pile_type: str | None = Field(None)
    photo_summary: str = Field("", max_length=1000)
    reporter_name: str = Field("", max_length=50)
    contact: str = Field("", max_length=100)
    happened_at: str | None = None
