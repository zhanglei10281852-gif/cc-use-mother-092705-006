from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# 业务周期：以“年份-季节”标识同一越冬周期；相同地点同一周期的登记合并为同一案件。
SeasonRisk = Literal["low", "medium", "high"]
DecisionType = Literal["retain", "adjust", "remove"]
ReviewKind = Literal["review", "complaint"]
UrgentAction = Literal["partial_clear", "full_clear", "fence", "other"]


class Site(BaseModel):
    """堆放位置：地点名称 + 可选坐标，作为同周期合并键的一部分。"""

    location_name: str = Field(..., min_length=1, max_length=200, description="堆放位置名称，如“中央公园北区落叶堆 A”")
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)


class RegistrationCreate(BaseModel):
    """现场登记（可由投诉触发）。相同地点 + 同一周期重复登记会合并到既有案件。"""

    site: Site
    period: str = Field(..., pattern=r"^\d{4}-(spring|summer|autumn|winter)$", description="生态周期，如 2026-winter")
    photo_summary: str = Field(..., min_length=1, max_length=2000, description="现场照片摘要（不接收原始图片）")
    pile_description: str = Field(default="", max_length=2000, description="落叶/枝堆形态、规模等描述")
    season_risk: SeasonRisk = Field("medium", description="季节风险：low 低 / medium 中 / high 高")
    observed_species: list[str] = Field(default_factory=list, description="现场观察到或疑似的动物，如刺猬、步甲")
    public_feedback: str = Field(default="", max_length=2000, description="公众反馈或投诉内容摘要")
    complainant_contact: str = Field(default="", max_length=100)
    source: Literal["patrol", "complaint", "hotline"] = "patrol"


class EcoBasis(BaseModel):
    """决定所引用的“当时的生态依据”，决定时一并固化，后续不可修改。"""

    reference_type: Literal["guideline", "survey", "expert_opinion", "local_manual"] = Field(
        ..., description="依据类型：规范指南 / 本底调查 / 专家意见 / 地方养护手册"
    )
    reference_name: str = Field(..., min_length=1, max_length=200, description="依据名称，如《城市野生动物栖息地管理指南》")
    reference_section: str = Field(default="", max_length=200, description="条款/章节")
    key_points: str = Field(..., min_length=1, max_length=2000, description="引用要点，说明为何保留/调整/移除")
    observed_evidence: str = Field(default="", max_length=2000, description="现场佐证（物种痕迹、温度、距步道距离等）")


class DecisionCreate(BaseModel):
    decision: DecisionType
    reason: str = Field(..., min_length=1, max_length=2000)
    eco_basis: EcoBasis
    adjustment_detail: str = Field(default="", max_length=2000, description="选择 adjust 时的具体措施（移到林缘、设标识牌等）")
    valid_until: Optional[str] = Field(None, description="决定有效期（建议在本周期结束前复查）")


class FollowUpCreate(BaseModel):
    """复查 / 投诉跟进：只能新增结论，旧记录保持不变。"""

    kind: ReviewKind = "review"
    conclusion: str = Field(..., min_length=1, max_length=2000)
    site_condition: str = Field(default="", max_length=2000, description="复查时现场状况")
    recommend_action: Optional[DecisionType] = None
    public_feedback: str = Field(default="", max_length=2000, description="本次新增的公众反馈（投诉模拟时填写）")


class EmergencyCreate(BaseModel):
    """紧急安全处置：可先执行再补录。"""

    action_taken: UrgentAction
    reason: str = Field(..., min_length=1, max_length=2000, description="紧急事由，如倒伏占道、火情、伤人风险")
    executed_at: str = Field(..., description="实际处置时间 ISO8601（补录时为过去时间）")
    executed_by: str = Field(..., min_length=1, max_length=100, description="现场处置责任人姓名")
    affected_scope: str = Field(default="", max_length=500, description="影响范围，如“全部清走/保留底层 30cm”")
    make_up_deadline_hours: int = Field(24, ge=1, le=168, description="补录期限（自处置时起，小时）")
    make_up_owner: str = Field(..., min_length=1, max_length=100, description="补录责任人")
    note: str = Field(default="", max_length=2000)
