from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.errors import PermissionDeniedError
from app.core.security import Principal
from app.database import get_connection
from app.habitat.schemas import DecisionCreate, EmergencyCreate, FollowUpCreate, RegistrationCreate
from app.habitat.service import HabitatService
from app.services.audit import AuditContext, AuditService

router = APIRouter(prefix="/api/habitat", tags=["栖息地保留台账"])

PERM_READ = "habitat.read"
PERM_REGISTER = "habitat.register"
PERM_DECIDE = "habitat.decide"
PERM_REVIEW = "habitat.review"
PERM_EMERGENCY = "habitat.emergency"


def require_permission(principal: Principal, permission: str, *, action: str, resource_id: int | None = None) -> None:
    """权限校验；拒绝时先落审计事件（denied）再抛出，保证越权可追溯。"""
    if principal.can(permission):
        return
    AuditService(get_connection()).record(
        AuditContext(principal.user_id, principal.display_name),
        action=action,
        resource_type="habitat_case",
        resource_id=resource_id,
        outcome="denied",
        metadata={"required_permission": permission},
    )
    raise PermissionDeniedError(f"缺少权限：{permission}")


@router.post("/cases/registrations", status_code=201)
def register_case(payload: RegistrationCreate, principal: Principal = Depends(current_principal)) -> dict:
    require_permission(principal, PERM_REGISTER, action="habitat.register")
    return HabitatService(get_connection()).register(principal, payload.model_dump())


@router.get("/cases")
def list_cases(
    status: str | None = None,
    period: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    require_permission(principal, PERM_READ, action="habitat.list")
    result = HabitatService(get_connection()).list_cases(
        status=status, period=period, limit=size, offset=(page - 1) * size
    )
    return {"page": page, "size": size, "total": result["total"], "data": result["data"]}


@router.get("/cases/{case_id}")
def get_case(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    require_permission(principal, PERM_READ, action="habitat.get", resource_id=case_id)
    return HabitatService(get_connection()).detail(case_id)


@router.post("/cases/{case_id}/decision", status_code=201)
def decide(case_id: int, payload: DecisionCreate, principal: Principal = Depends(current_principal)) -> dict:
    # 仅授权人员可决定保留/调整/移除。
    require_permission(principal, PERM_DECIDE, action="habitat.decide", resource_id=case_id)
    return HabitatService(get_connection()).decide(principal, case_id, payload.model_dump())


@router.post("/cases/{case_id}/followups", status_code=201)
def add_followup(case_id: int, payload: FollowUpCreate, principal: Principal = Depends(current_principal)) -> dict:
    require_permission(principal, PERM_REVIEW, action="habitat.review", resource_id=case_id)
    return HabitatService(get_connection()).add_followup(principal, case_id, payload.model_dump())


@router.post("/cases/{case_id}/emergencies", status_code=201)
def record_emergency(case_id: int, payload: EmergencyCreate, principal: Principal = Depends(current_principal)) -> dict:
    require_permission(principal, PERM_EMERGENCY, action="habitat.emergency", resource_id=case_id)
    return HabitatService(get_connection()).record_emergency(principal, case_id, payload.model_dump())
