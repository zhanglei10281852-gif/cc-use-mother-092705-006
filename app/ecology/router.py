from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.ecology.schemas import (
    ComplaintCreate,
    DecisionCreate,
    EmergencyCreate,
    RegistrationCreate,
    ReviewCreate,
    SupplementCreate,
)
from app.ecology.service import EcologyService, ensure_schema

router = APIRouter(prefix="/api/ecology/cases", tags=["生态保留台账"])


def service() -> EcologyService:
    ensure_schema()
    return EcologyService(get_connection())


def _require(principal: Principal, permission: str, action: str, resource_id: int | None = None) -> None:
    """在业务事务开启前完成鉴权，保证拒绝审计事件独立落库。"""
    service().require_perm(principal, permission, action, resource_id=resource_id)


@router.post("/complaints", status_code=201)
def submit_complaint(payload: ComplaintCreate) -> dict:
    """公众投诉入口：无需登录，自动并入同地点同周期案件。"""
    with transaction(immediate=True) as connection:
        case = EcologyService(connection).add_complaint(payload.model_dump())
    return {"case_id": case["id"], "case_no": case["case_no"], "merged": case.get("merged", False), "case": case}


@router.post("/registrations", status_code=201)
def register_case(payload: RegistrationCreate, principal: Principal = Depends(current_principal)) -> dict:
    _require(principal, "eco.register", "eco.register.denied")
    with transaction(immediate=True) as connection:
        case = EcologyService(connection).register(principal, payload.model_dump())
    return {"case_id": case["id"], "case_no": case["case_no"], "merged": case.get("merged", False), "case": case}


@router.post("/emergencies", status_code=201)
def record_emergency(payload: EmergencyCreate, principal: Principal = Depends(current_principal)) -> dict:
    """紧急安全处置先执行后补录，必须填写补录期限与责任人。"""
    _require(principal, "eco.emergency", "eco.emergency.denied")
    with transaction(immediate=True) as connection:
        case = EcologyService(connection).record_emergency(principal, payload.model_dump())
    return {"case_id": case["id"], "case_no": case["case_no"], "merged": case.get("merged", False), "case": case}


@router.get("")
def list_cases(
    status: str | None = None,
    site_name: str | None = None,
    period: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    ecology = service()
    ecology.require_perm(principal, "eco.read", "eco.read.denied")
    result = ecology.list_cases(
        status=status, site_name=site_name, period=period,
        limit=size, offset=(page - 1) * size,
    )
    result.update({"page": page, "size": size})
    return result


@router.get("/emergencies/overdue")
def overdue_supplements(principal: Principal = Depends(current_principal)) -> dict:
    ecology = service()
    ecology.require_perm(principal, "eco.read", "eco.read.denied")
    return {"data": ecology.overdue_supplements()}


@router.get("/{case_id}")
def get_case(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    ecology = service()
    ecology.require_perm(principal, "eco.read", "eco.read.denied", resource_id=case_id)
    return ecology.detail(case_id)


@router.post("/{case_id}/decisions", status_code=201)
def decide(case_id: int, payload: DecisionCreate, principal: Principal = Depends(current_principal)) -> dict:
    _require(principal, "eco.decide", "eco.decide.denied", resource_id=case_id)
    with transaction(immediate=True) as connection:
        case = EcologyService(connection).decide(principal, case_id, payload.model_dump())
    return {"case_id": case_id, "case": case}


@router.post("/{case_id}/reviews", status_code=201)
def add_review(case_id: int, payload: ReviewCreate, principal: Principal = Depends(current_principal)) -> dict:
    """复查结论只追加，不覆盖案件状态和历史结论。"""
    _require(principal, "eco.review", "eco.review.denied", resource_id=case_id)
    with transaction(immediate=True) as connection:
        case = EcologyService(connection).add_review(principal, case_id, payload.model_dump())
    return {"case_id": case_id, "case": case}


@router.post("/{case_id}/supplement", status_code=201)
def supplement(case_id: int, payload: SupplementCreate, principal: Principal = Depends(current_principal)) -> dict:
    _require(principal, "eco.emergency", "eco.emergency.supplement.denied", resource_id=case_id)
    with transaction(immediate=True) as connection:
        case = EcologyService(connection).supplement(principal, case_id, payload.model_dump())
    return {"case_id": case_id, "case": case}
