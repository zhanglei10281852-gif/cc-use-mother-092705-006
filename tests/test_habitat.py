from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _make_user(client, admin, username, permissions):
    role_code = f"role_{username}"
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": role_code, "name": role_code, "permission_codes": permissions},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Habitat!23456", "display_name": username, "role_codes": [role_code]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Habitat!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


REGISTRATION = {
    "site": {"location_name": "中央公园北区落叶堆 A", "latitude": 39.9, "longitude": 116.4},
    "period": "2026-winter",
    "photo_summary": "三张照片：栎树落叶厚约 20cm，内有刺猬活动痕迹",
    "pile_description": "林缘落叶与断枝混合堆，约 4 平米",
    "season_risk": "high",
    "observed_species": ["刺猬", "步甲"],
    "public_feedback": "居民反映看起来杂乱",
    "source": "patrol",
}

ECO_BASIS = {
    "reference_type": "guideline",
    "reference_name": "城市野生动物栖息地管理指南",
    "reference_section": "第 4.2 条 越冬覆盖物",
    "key_points": "11 月至次年 3 月的落叶枝堆为刺猬等越冬动物提供覆盖，不应在越冬期清理",
    "observed_evidence": "堆内发现刺猬冬眠巢，距主步道 6 米",
}


def _register(client, headers, **overrides):
    payload = {**REGISTRATION, **overrides}
    response = client.post("/api/habitat/cases/registrations", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_full_register_decide_review_flow(client, admin):
    officer = _make_user(client, admin, "officer1", ["habitat.read", "habitat.register", "habitat.decide", "habitat.review"])

    created = _register(client, officer)
    case_id = created["id"]
    assert created["status"] == "registered"
    assert created["merged"] is False
    assert created["registrations"][0]["observed_species"] == ["刺猬", "步甲"]

    # 复查只能新增结论：先补一条复查，旧登记仍在
    review = client.post(
        f"/api/habitat/cases/{case_id}/followups",
        headers=officer,
        json={"kind": "review", "conclusion": "落叶堆保持稳定，建议保留越冬", "site_condition": "无明显霉变"},
    )
    assert review.status_code == 201, review.text
    assert review.json()["status"] == "registered"

    # 决定保留：必须引用当时的生态依据
    decision = client.post(
        f"/api/habitat/cases/{case_id}/decision",
        headers=officer,
        json={"decision": "retain", "reason": "越冬期栖息地", "eco_basis": ECO_BASIS, "valid_until": "2027-03-15"},
    )
    assert decision.status_code == 201, decision.text
    body = decision.json()
    assert body["status"] == "retained"
    assert body["current_decision"]["eco_basis"]["reference_name"] == "城市野生动物栖息地管理指南"
    assert body["current_decision"]["seq"] == 1

    # 再次决定（移除）只能追加：旧决定保留不变
    second = client.post(
        f"/api/habitat/cases/{case_id}/decision",
        headers=officer,
        json={"decision": "remove", "reason": "越冬期结束", "eco_basis": {**ECO_BASIS, "key_points": "3 月中旬后动物结束冬眠，可恢复常规保洁"}},
    )
    assert second.status_code == 201
    detail = client.get(f"/api/habitat/cases/{case_id}", headers=officer).json()
    assert [d["seq"] for d in detail["decisions"]] == [1, 2]
    assert detail["decisions"][0]["decision"] == "retain"
    assert detail["decisions"][1]["decision"] == "remove"
    assert detail["status"] == "removed"
    assert len(detail["followups"]) == 1


def test_duplicate_registration_same_site_and_period_merges(client, admin):
    officer = _make_user(client, admin, "officer2", ["habitat.read", "habitat.register"])

    first = _register(client, officer)
    # 同地点（仅空白/大小写差异）同周期 → 合并
    second = _register(
        client,
        officer,
        site={"location_name": "  中央公园北区落叶堆   a ", "latitude": 39.9, "longitude": 116.4},
        photo_summary="巡查再次拍照：规模略增",
        public_feedback="12345 热线投诉称影响观感",
        source="hotline",
    )
    assert second["merged"] is True
    assert second["id"] == first["id"]
    assert second["registration_count"] == 2

    # 不同周期 → 新案件
    other_period = _register(client, officer, period="2027-spring", photo_summary="春季新堆")
    assert other_period["id"] != first["id"]

    listing = client.get("/api/habitat/cases?period=2026-winter", headers=officer)
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


def test_complaint_moves_case_under_review(client, admin):
    officer = _make_user(client, admin, "officer3", ["habitat.read", "habitat.register", "habitat.decide", "habitat.review"])
    case_id = _register(client, officer)["id"]
    client.post(
        f"/api/habitat/cases/{case_id}/decision",
        headers=officer,
        json={"decision": "retain", "reason": "越冬栖息", "eco_basis": ECO_BASIS},
    )

    complaint = client.post(
        f"/api/habitat/cases/{case_id}/followups",
        headers=officer,
        json={"kind": "complaint", "conclusion": "居民再次投诉落叶堆积影响儿童活动", "public_feedback": "希望尽快清理"},
    )
    assert complaint.status_code == 201, complaint.text
    assert complaint.json()["status"] == "under_review"

    # 授权人复查后调整
    adjust = client.post(
        f"/api/habitat/cases/{case_id}/decision",
        headers=officer,
        json={
            "decision": "adjust",
            "reason": "兼顾栖息与观感",
            "eco_basis": ECO_BASIS,
            "adjustment_detail": "移至林缘 10 米处并设置解说标识牌，保留底层 30cm",
        },
    )
    assert adjust.status_code == 201
    assert adjust.json()["status"] == "adjusted"


def test_decision_requires_eco_basis_and_adjust_detail(client, admin):
    officer = _make_user(client, admin, "officer4", ["habitat.read", "habitat.register", "habitat.decide"])
    case_id = _register(client, officer)["id"]

    missing_basis = client.post(
        f"/api/habitat/cases/{case_id}/decision",
        headers=officer,
        json={"decision": "retain", "reason": "x"},
    )
    assert missing_basis.status_code == 422

    adjust_without_detail = client.post(
        f"/api/habitat/cases/{case_id}/decision",
        headers=officer,
        json={"decision": "adjust", "reason": "x", "eco_basis": ECO_BASIS},
    )
    assert adjust_without_detail.status_code == 422


def test_emergency_disposal_makeup_within_deadline(client, admin):
    officer = _make_user(client, admin, "officer5", ["habitat.read", "habitat.register", "habitat.emergency"])
    case_id = _register(client, officer)["id"]

    executed = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    response = client.post(
        f"/api/habitat/cases/{case_id}/emergencies",
        headers=officer,
        json={
            "action_taken": "partial_clear",
            "reason": "倒伏树枝阻断主步道，存在行人伤害风险",
            "executed_at": executed,
            "executed_by": "班组现场负责人王强",
            "affected_scope": "清走路面断枝，保留林缘底层落叶",
            "make_up_deadline_hours": 24,
            "make_up_owner": "台账员李敏",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "emergency_disposed"
    emergency = body["emergencies"][-1]
    assert emergency["make_up_owner"] == "台账员李敏"
    assert emergency["make_up_status"] == "on_time"
    assert emergency["make_up_deadline"] > emergency["executed_at"]


def test_emergency_disposal_makeup_overdue(client, admin):
    officer = _make_user(client, admin, "officer6", ["habitat.read", "habitat.register", "habitat.emergency"])
    case_id = _register(client, officer)["id"]

    executed = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    response = client.post(
        f"/api/habitat/cases/{case_id}/emergencies",
        headers=officer,
        json={
            "action_taken": "full_clear",
            "reason": "火情隐患",
            "executed_at": executed,
            "executed_by": "王强",
            "make_up_deadline_hours": 24,
            "make_up_owner": "李敏",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "makeup_overdue"


def test_permission_denied_and_audit_events(client, admin):
    # 仅有登记权限，不能决定、复查、补录紧急处置
    clerk = _make_user(client, admin, "clerk_h", ["habitat.read", "habitat.register"])
    case_id = _register(client, clerk)["id"]

    decide = client.post(
        f"/api/habitat/cases/{case_id}/decision",
        headers=clerk,
        json={"decision": "retain", "reason": "x", "eco_basis": ECO_BASIS},
    )
    assert decide.status_code == 403

    review = client.post(
        f"/api/habitat/cases/{case_id}/followups",
        headers=clerk,
        json={"kind": "review", "conclusion": "x"},
    )
    assert review.status_code == 403

    emergency = client.post(
        f"/api/habitat/cases/{case_id}/emergencies",
        headers=clerk,
        json={
            "action_taken": "fence",
            "reason": "x",
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "executed_by": "x",
            "make_up_owner": "x",
        },
    )
    assert emergency.status_code == 403

    # 案件状态未因越权而改变
    detail = client.get(f"/api/habitat/cases/{case_id}", headers=clerk).json()
    assert detail["status"] == "registered"

    # 管理员查看审计：越权拒绝均有 denied 事件
    denied = client.get(
        "/api/audit?resource_type=habitat_case&outcome=denied",
        headers=admin["headers"],
    )
    assert denied.status_code == 200
    actions = {event["action"] for event in denied.json()["data"]}
    assert {"habitat.decide", "habitat.review", "habitat.emergency"} <= actions

    # 成功登记也有 success 审计
    success = client.get(
        "/api/audit?resource_type=habitat_case&outcome=success&action=habitat.register",
        headers=admin["headers"],
    )
    assert success.json()["total"] >= 1

    # 合并登记有专门审计动作
    merged = client.get(
        "/api/audit?resource_type=habitat_case&action=habitat.register.merged",
        headers=admin["headers"],
    )
    # 本案未合并，应为 0；上面合并用例的事件不在本库，这里只验证接口可过滤
    assert merged.status_code == 200


def test_authentication_required(client):
    response = client.post(
        "/api/habitat/cases/registrations",
        json=REGISTRATION,
    )
    assert response.status_code == 401
