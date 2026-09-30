from __future__ import annotations

import pytest


SITE = {"site_name": "滨河公园北门", "location_text": "北门樟树林东侧 5 米落叶带", "pile_type": "落叶"}


def _login(client, username: str, password: str) -> dict:
    response = client.post("/api/auth/login", json={"username": "admin", "password": password} if username == "admin"
                           else {"username": username, "password": password, "client_label": "tests"})
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture()
def users(client, admin):
    staff = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "eco.staff", "password": "Staff!234567", "display_name": "清洁班组员", "role_codes": ["clerk"]},
    )
    assert staff.status_code == 201, staff.text
    manager = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "eco.boss", "password": "Boss!2345678", "display_name": "生态授权人", "role_codes": ["eco_manager"]},
    )
    assert manager.status_code == 201, manager.text
    readonly = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "eco.audit", "password": "Audit!234567", "display_name": "审计员", "role_codes": ["auditor"]},
    )
    assert readonly.status_code == 201, readonly.text
    return {
        "staff": _login(client, "eco.staff", "Staff!234567"),
        "manager": _login(client, "eco.boss", "Boss!2345678"),
        "readonly": _login(client, "eco.audit", "Audit!234567"),
    }


def _register(client, auth, **overrides):
    payload = {
        "photo_summary": "照片 3 张：厚层落叶覆盖树根，有刺猬活动痕迹",
        "season_risk": "12 月越冬期，落叶层是刺猬与昆虫越冬栖所",
        "risk_level": "中",
        "happened_at": "2026-12-05T09:00:00+00:00",
        **SITE,
        **overrides,
    }
    return client.post("/api/ecology/cases/registrations", headers=auth["headers"], json=payload)


def test_registration_creates_pending_case(client, users):
    response = _register(client, users["staff"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["merged"] is False
    assert body["case"]["status"] == "待决定"
    assert body["case"]["period"].endswith("越冬周期")
    assert body["case_no"].startswith("ECO-")


def test_duplicate_registration_same_location_same_period_merges(client, users):
    first = _register(client, users["staff"]).json()
    second = _register(client, users["staff"], photo_summary="第二次巡查：落叶增厚")
    assert second.status_code == 201, second.text
    assert second.json()["merged"] is True
    assert second.json()["case_id"] == first["case_id"]
    detail = client.get(f"/api/ecology/cases/{first['case_id']}", headers=users["staff"]["headers"]).json()
    assert len(detail["registrations"]) == 2
    # 列表中只出现一个案件
    listing = client.get("/api/ecology/cases", headers=users["manager"]["headers"]).json()
    assert listing["total"] == 1


def test_different_period_does_not_merge(client, users):
    winter = _register(client, users["staff"], happened_at="2026-12-15T09:00:00+00:00").json()
    summer = _register(client, users["staff"], happened_at="2026-06-15T09:00:00+00:00")
    assert summer.json()["merged"] is False
    assert summer.json()["case_id"] != winter["case_id"]
    assert summer.json()["case"]["period"] == "2026年常规周期"


def test_jan_feb_share_winter_period(client, users):
    nov = _register(client, users["staff"], happened_at="2026-11-20T09:00:00+00:00").json()
    jan = _register(client, users["staff"], happened_at="2027-01-10T09:00:00+00:00")
    assert jan.json()["merged"] is True
    assert jan.json()["case_id"] == nov["case_id"]


def test_decision_requires_permission_and_writes_denied_audit(client, users, admin):
    case_id = _register(client, users["staff"]).json()["case_id"]
    # 班组员没有决定权
    denied = client.post(
        f"/api/ecology/cases/{case_id}/decisions",
        headers=users["staff"]["headers"],
        json={"decision": "保留", "ecological_basis": "《城市绿地生态管护导则》：越冬期保留落叶层"},
    )
    assert denied.status_code == 403, denied.text
    events = client.get("/api/audit?action=eco.decide.denied&outcome=denied", headers=admin["headers"])
    assert events.status_code == 200
    assert events.json()["total"] == 1
    # 案件状态未改变
    detail = client.get(f"/api/ecology/cases/{case_id}", headers=users["readonly"]["headers"]).json()
    assert detail["status"] == "待决定"


def test_authorized_decision_records_basis_and_status(client, users):
    case_id = _register(client, users["staff"]).json()["case_id"]
    response = client.post(
        f"/api/ecology/cases/{case_id}/decisions",
        headers=users["manager"]["headers"],
        json={
            "decision": "保留",
            "ecological_basis": "落叶层为刺猬越冬栖息地，11 月至次年 2 月禁止清除",
            "basis_refs": ["《城市绿地生态管护导则》第 7.2 条", "现场红外记录 IR-0231"],
            "review_due_at": "2027-02-20T10:00:00+00:00",
        },
    )
    assert response.status_code == 201, response.text
    case = response.json()["case"]
    assert case["status"] == "保留中"
    assert case["current_decision"] == "保留"
    decision = case["decisions"][0]
    assert decision["ecological_basis"].startswith("落叶层为刺猬")
    assert decision["basis_refs"][1] == "现场红外记录 IR-0231"
    assert decision["decided_by"] == "生态授权人"


def test_adjust_decision_requires_measures(client, users):
    case_id = _register(client, users["staff"]).json()["case_id"]
    response = client.post(
        f"/api/ecology/cases/{case_id}/decisions",
        headers=users["manager"]["headers"],
        json={"decision": "调整", "ecological_basis": "落叶堆部分压占步道"},
    )
    assert response.status_code == 422


def test_review_only_appends_and_never_overwrites(client, users, admin):
    case_id = _register(client, users["staff"]).json()["case_id"]
    client.post(
        f"/api/ecology/cases/{case_id}/decisions",
        headers=users["manager"]["headers"],
        json={"decision": "保留", "ecological_basis": "越冬栖息地依据 B-1"},
    )
    first = client.post(
        f"/api/ecology/cases/{case_id}/reviews",
        headers=users["staff"]["headers"],
        json={"conclusion_type": "继续观察", "finding": "刺猬仍在活动，建议维持", "reviewed_at": "2026-12-20T10:00:00+00:00"},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/ecology/cases/{case_id}/reviews",
        headers=users["staff"]["headers"],
        json={"conclusion_type": "维持保留", "finding": "栖息痕迹稳定", "reviewed_at": "2027-01-15T10:00:00+00:00"},
    )
    assert second.status_code == 201
    detail = client.get(f"/api/ecology/cases/{case_id}", headers=users["manager"]["headers"]).json()
    # 复查不覆盖决定状态，决定仍在，复查按序号追加
    assert detail["status"] == "保留中"
    assert len(detail["decisions"]) == 1
    assert [item["seq"] for item in detail["reviews"]] == [1, 2]
    assert detail["reviews"][0]["finding"] == "刺猬仍在活动，建议维持"
    events = client.get("/api/audit?action=eco.review.append", headers=admin["headers"]).json()
    assert events["total"] == 2


def test_review_records_cannot_be_modified_at_storage(client, users):
    from app.database import get_connection

    case_id = _register(client, users["staff"]).json()["case_id"]
    client.post(
        f"/api/ecology/cases/{case_id}/decisions",
        headers=users["manager"]["headers"],
        json={"decision": "保留", "ecological_basis": "依据"},
    )
    client.post(
        f"/api/ecology/cases/{case_id}/reviews",
        headers=users["staff"]["headers"],
        json={"conclusion_type": "继续观察", "finding": "原始结论"},
    )
    connection = get_connection()
    with pytest.raises(Exception):
        connection.execute("UPDATE eco_reviews SET finding='被篡改' WHERE case_id=?", (case_id,))
    with pytest.raises(Exception):
        connection.execute("DELETE FROM eco_decisions WHERE case_id=?", (case_id,))


def test_emergency_then_supplement_flow(client, users):
    response = client.post(
        "/api/ecology/cases/emergencies",
        headers=users["staff"]["headers"],
        json={
            **SITE,
            "action_taken": "已清除压占消防通道的枝堆",
            "reason": "大风刮断树枝阻断消防通道",
            "hazard_type": "通道阻断",
            "responsible_person": "班组长老王",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    case = body["case"]
    assert case["status"] == "紧急处置待补录"
    emergency = case["emergencies"][0]
    assert emergency["responsible_person"] == "班组长老王"
    assert emergency["supplement_status"] == "待补录"
    assert emergency["supplement_deadline"] > emergency["executed_at"]
    case_id = body["case_id"]

    supplemented = client.post(
        f"/api/ecology/cases/{case_id}/supplement",
        headers=users["staff"]["headers"],
        json={
            "pile_type": "枝堆",
            "photo_summary": "补传处置前后照片摘要",
            "season_risk": "非主要越冬栖所，周边 20 米有替代落叶带",
            "risk_level": "高",
        },
    )
    assert supplemented.status_code == 201, supplemented.text
    result = supplemented.json()["case"]
    assert result["status"] == "待决定"
    assert result["emergencies"][0]["supplement_status"] == "已补录"


def test_emergency_merges_into_existing_case_same_period(client, users):
    case_id = _register(client, users["staff"]).json()["case_id"]
    response = client.post(
        "/api/ecology/cases/emergencies",
        headers=users["staff"]["headers"],
        json={
            **SITE,
            "action_taken": "局部清运过火落叶",
            "reason": "发现冒烟火点",
            "responsible_person": "班组长老王",
            "happened_at": "2026-12-06T09:00:00+00:00",
        },
    )
    assert response.status_code == 201
    assert response.json()["merged"] is True
    assert response.json()["case_id"] == case_id


def test_overdue_supplement_listing(client, users, admin):
    created = client.post(
        "/api/ecology/cases/emergencies",
        headers=users["staff"]["headers"],
        json={
            **SITE,
            "action_taken": "紧急清运",
            "reason": "积水浸泡步道",
            "responsible_person": "责任人赵六",
            "supplement_deadline": "2020-01-01T00:00:00+00:00",
            "happened_at": "2019-12-30T00:00:00+00:00",
        },
    )
    assert created.status_code == 201, created.text
    overdue = client.get("/api/ecology/cases/emergencies/overdue", headers=users["manager"]["headers"])
    assert overdue.status_code == 200
    assert len(overdue.json()["data"]) == 1
    # 逾期后补录应标记为“已逾期补录”
    case_id = created.json()["case_id"]
    late = client.post(
        f"/api/ecology/cases/{case_id}/supplement",
        headers=users["staff"]["headers"],
        json={"pile_type": "落叶", "season_risk": "补录", "risk_level": "低"},
    )
    assert late.status_code == 201
    assert late.json()["case"]["emergencies"][0]["supplement_status"] == "已逾期补录"


def test_public_complaint_creates_and_merges_without_auth(client, users):
    payload = {**SITE, "content": "北门落叶长期没人清，看起来很乱", "reporter_name": "张女士"}
    first = client.post("/api/ecology/cases/complaints", json=payload)
    assert first.status_code == 201, first.text
    case_id = first.json()["case_id"]
    again = client.post(
        "/api/ecology/cases/complaints",
        json={**SITE, "content": "又有居民投诉同一处", "reporter_name": "李先生"},
    )
    assert again.json()["merged"] is True
    assert again.json()["case_id"] == case_id
    detail = client.get(f"/api/ecology/cases/{case_id}", headers=users["manager"]["headers"]).json()
    assert len(detail["feedback"]) == 2
    assert detail["status"] == "待决定"


def test_read_requires_authentication_and_permission(client, users):
    # 未登录
    assert client.get("/api/ecology/cases").status_code == 401
    # 审计只读角色可以看，但没有任何写权限
    ok = client.get("/api/ecology/cases", headers=users["readonly"]["headers"])
    assert ok.status_code == 200
    denied = client.post(
        "/api/ecology/cases/registrations",
        headers=users["readonly"]["headers"],
        json={**SITE, "photo_summary": "x"},
    )
    assert denied.status_code == 403


def test_remove_decision_closes_followup_merging(client, users):
    case_id = _register(client, users["staff"]).json()["case_id"]
    decision = client.post(
        f"/api/ecology/cases/{case_id}/decisions",
        headers=users["manager"]["headers"],
        json={"decision": "移除", "ecological_basis": "经核查无越冬动物迹象，且紧邻变电设施"},
    )
    assert decision.status_code == 201
    assert decision.json()["case"]["status"] == "已移除"
    # 同周期再次登记同一地点会另立案件（旧案件已移除）
    followup = _register(client, users["staff"], photo_summary="新堆积")
    assert followup.json()["merged"] is False
    assert followup.json()["case_id"] != case_id
