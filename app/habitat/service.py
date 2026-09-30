from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.services.audit import AuditContext, AuditService

# 案件状态：登记后待决定；决定后进入保留/调整/移除；投诉触发复查中；紧急处置后紧急处置/补录逾期。
STATUS_REGISTERED = "registered"
STATUS_RETAINED = "retained"
STATUS_ADJUSTED = "adjusted"
STATUS_REMOVED = "removed"
STATUS_UNDER_REVIEW = "under_review"
STATUS_EMERGENCY = "emergency_disposed"
STATUS_MAKEUP_OVERDUE = "makeup_overdue"

DECISION_STATUSES = {"retain": STATUS_RETAINED, "adjust": STATUS_ADJUSTED, "remove": STATUS_REMOVED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS habitat_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_key TEXT NOT NULL,
    location_name TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    period TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'registered' CHECK(status IN (
        'registered','retained','adjusted','removed','under_review','emergency_disposed','makeup_overdue'
    )),
    pile_description TEXT NOT NULL DEFAULT '',
    season_risk TEXT NOT NULL DEFAULT 'medium' CHECK(season_risk IN ('low','medium','high')),
    current_decision_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(location_key, period)
);
CREATE INDEX IF NOT EXISTS idx_habitat_cases_status ON habitat_cases(status, updated_at);

CREATE TABLE IF NOT EXISTS habitat_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES habitat_cases(id) ON DELETE RESTRICT,
    photo_summary TEXT NOT NULL,
    pile_description TEXT NOT NULL DEFAULT '',
    season_risk TEXT NOT NULL CHECK(season_risk IN ('low','medium','high')),
    observed_species_json TEXT NOT NULL DEFAULT '[]',
    public_feedback TEXT NOT NULL DEFAULT '',
    complainant_contact TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK(source IN ('patrol','complaint','hotline')),
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_registrations_case ON habitat_registrations(case_id, id);

CREATE TABLE IF NOT EXISTS habitat_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES habitat_cases(id) ON DELETE RESTRICT,
    seq INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('retain','adjust','remove')),
    reason TEXT NOT NULL,
    eco_basis_json TEXT NOT NULL,
    adjustment_detail TEXT NOT NULL DEFAULT '',
    valid_until TEXT,
    decided_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_habitat_decisions_case ON habitat_decisions(case_id, seq);

CREATE TABLE IF NOT EXISTS habitat_followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES habitat_cases(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('review','complaint')),
    conclusion TEXT NOT NULL,
    site_condition TEXT NOT NULL DEFAULT '',
    recommend_action TEXT CHECK(recommend_action IS NULL OR recommend_action IN ('retain','adjust','remove')),
    public_feedback TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_followups_case ON habitat_followups(case_id, id);

CREATE TABLE IF NOT EXISTS habitat_emergencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES habitat_cases(id) ON DELETE RESTRICT,
    action_taken TEXT NOT NULL CHECK(action_taken IN ('partial_clear','full_clear','fence','other')),
    reason TEXT NOT NULL,
    executed_at TEXT NOT NULL,
    executed_by TEXT NOT NULL,
    affected_scope TEXT NOT NULL DEFAULT '',
    make_up_deadline TEXT NOT NULL,
    make_up_owner TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    make_up_status TEXT NOT NULL CHECK(make_up_status IN ('on_time','overdue')),
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_habitat_emergencies_case ON habitat_emergencies(case_id, id);
"""


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def location_key(location_name: str) -> str:
    """合并键：同一地点的归一化名称（忽略首尾及多余空白、大小写）。"""
    return re.sub(r"\s+", " ", location_name.strip()).casefold()


def _parse_executed_at(value: str) -> str:
    parsed = from_storage(value)
    if parsed is None:
        raise ValidationError("处置时间格式无效")
    return to_storage(parsed)


class HabitatService:
    """保留决策台账：登记合并、授权决定（引用生态依据）、复查只增不改、紧急处置先做后补。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    # ---- 登记与合并 ----

    def register(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        site = payload["site"]
        key = location_key(site["location_name"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            case = connection.execute(
                "SELECT * FROM habitat_cases WHERE location_key=? AND period=?",
                (key, payload["period"]),
            ).fetchone()
            merged = case is not None
            if case is None:
                cursor = connection.execute(
                    "INSERT INTO habitat_cases(location_key,location_name,latitude,longitude,period,status,"
                    "pile_description,season_risk,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        site["location_name"].strip(),
                        site.get("latitude"),
                        site.get("longitude"),
                        payload["period"],
                        STATUS_REGISTERED,
                        payload.get("pile_description", ""),
                        payload["season_risk"],
                        now,
                        now,
                    ),
                )
                case_id = int(cursor.lastrowid)
            else:
                case_id = int(case["id"])
                # 重复登记并入同一案件：更新最新风险与描述，但不覆盖任何历史登记行。
                connection.execute(
                    "UPDATE habitat_cases SET season_risk=?, pile_description=?, updated_at=? WHERE id=?",
                    (payload["season_risk"], payload.get("pile_description", "") or case["pile_description"], now, case_id),
                )
            cursor = connection.execute(
                "INSERT INTO habitat_registrations(case_id,photo_summary,pile_description,season_risk,"
                "observed_species_json,public_feedback,complainant_contact,source,registered_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    payload["photo_summary"],
                    payload.get("pile_description", ""),
                    payload["season_risk"],
                    json.dumps(payload.get("observed_species", []), ensure_ascii=False),
                    payload.get("public_feedback", ""),
                    payload.get("complainant_contact", ""),
                    payload["source"],
                    principal.display_name,
                    now,
                ),
            )
            registration_id = int(cursor.lastrowid)
            result = self.detail(case_id)
            result["merged"] = merged
            result["registration_id"] = registration_id
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="habitat.register.merged" if merged else "habitat.register",
                resource_type="habitat_case",
                resource_id=case_id,
                after={"period": payload["period"], "location": site["location_name"].strip(), "merged": merged},
                metadata={"registration_id": registration_id, "source": payload["source"]},
            )
            return result

    # ---- 授权决定（必须引用当时的生态依据） ----

    def decide(self, principal: Principal, case_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            case = self._require_case(connection, case_id)
            decision_value = payload["decision"]
            if decision_value == "adjust" and not payload.get("adjustment_detail", "").strip():
                raise ValidationError("选择“调整”时必须填写具体调整措施")
            now = to_storage(self.clock.now())
            seq = int(connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM habitat_decisions WHERE case_id=?", (case_id,)
            ).fetchone()[0])
            eco_basis = payload["eco_basis"]
            cursor = connection.execute(
                "INSERT INTO habitat_decisions(case_id,seq,decision,reason,eco_basis_json,adjustment_detail,"
                "valid_until,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    seq,
                    decision_value,
                    payload["reason"],
                    json.dumps(eco_basis, ensure_ascii=False, sort_keys=True),
                    payload.get("adjustment_detail", ""),
                    payload.get("valid_until"),
                    principal.display_name,
                    now,
                ),
            )
            decision_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE habitat_cases SET status=?, current_decision_id=?, updated_at=? WHERE id=?",
                (DECISION_STATUSES[decision_value], decision_id, now, case_id),
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="habitat.decide",
                resource_type="habitat_case",
                resource_id=case_id,
                before={"status": case["status"]},
                after={"status": DECISION_STATUSES[decision_value], "decision_id": decision_id, "decision": decision_value},
                metadata={"seq": seq, "eco_reference": eco_basis["reference_name"], "eco_section": eco_basis.get("reference_section", "")},
            )
            return self.detail(case_id)

    # ---- 复查 / 投诉：只新增结论，不覆盖旧记录 ----

    def add_followup(self, principal: Principal, case_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            case = self._require_case(connection, case_id)
            now = to_storage(self.clock.now())
            cursor = connection.execute(
                "INSERT INTO habitat_followups(case_id,kind,conclusion,site_condition,recommend_action,"
                "public_feedback,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    payload["kind"],
                    payload["conclusion"],
                    payload.get("site_condition", ""),
                    payload.get("recommend_action"),
                    payload.get("public_feedback", ""),
                    principal.display_name,
                    now,
                ),
            )
            followup_id = int(cursor.lastrowid)
            # 投诉使案件回到“复查中”，等待授权人重新决定；普通复查结论不改动既有决定状态。
            if payload["kind"] == "complaint":
                connection.execute(
                    "UPDATE habitat_cases SET status=?, updated_at=? WHERE id=?",
                    (STATUS_UNDER_REVIEW, now, case_id),
                )
            else:
                connection.execute("UPDATE habitat_cases SET updated_at=? WHERE id=?", (now, case_id))
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="habitat.complaint" if payload["kind"] == "complaint" else "habitat.review",
                resource_type="habitat_case",
                resource_id=case_id,
                before={"status": case["status"]},
                after={"status": STATUS_UNDER_REVIEW if payload["kind"] == "complaint" else case["status"]},
                metadata={"followup_id": followup_id},
            )
            result = self.detail(case_id)
            result["followup_id"] = followup_id
            return result

    # ---- 紧急安全处置：先执行，再补录，标明期限与责任人 ----

    def record_emergency(self, principal: Principal, case_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            case = self._require_case(connection, case_id)
            now_dt = self.clock.now()
            executed_at = _parse_executed_at(payload["executed_at"])
            executed_dt = from_storage(executed_at)
            assert executed_dt is not None
            deadline_dt = executed_dt + timedelta(hours=payload["make_up_deadline_hours"])
            make_up_deadline = to_storage(deadline_dt)
            make_up_status = "overdue" if now_dt > deadline_dt else "on_time"
            now = to_storage(now_dt)
            cursor = connection.execute(
                "INSERT INTO habitat_emergencies(case_id,action_taken,reason,executed_at,executed_by,"
                "affected_scope,make_up_deadline,make_up_owner,recorded_at,make_up_status,note,recorded_by) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    payload["action_taken"],
                    payload["reason"],
                    executed_at,
                    payload["executed_by"],
                    payload.get("affected_scope", ""),
                    make_up_deadline,
                    payload["make_up_owner"],
                    now,
                    make_up_status,
                    payload.get("note", ""),
                    principal.display_name,
                ),
            )
            emergency_id = int(cursor.lastrowid)
            new_status = STATUS_MAKEUP_OVERDUE if make_up_status == "overdue" else STATUS_EMERGENCY
            connection.execute(
                "UPDATE habitat_cases SET status=?, updated_at=? WHERE id=?",
                (new_status, now, case_id),
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="habitat.emergency",
                resource_type="habitat_case",
                resource_id=case_id,
                before={"status": case["status"]},
                after={"status": new_status, "emergency_id": emergency_id},
                metadata={
                    "executed_at": executed_at,
                    "executed_by": payload["executed_by"],
                    "make_up_deadline": make_up_deadline,
                    "make_up_owner": payload["make_up_owner"],
                    "make_up_status": make_up_status,
                },
            )
            result = self.detail(case_id)
            result["emergency_id"] = emergency_id
            return result

    # ---- 查询 ----

    def list_cases(self, *, status: str | None = None, period: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if period:
            conditions.append("period=?")
            params.append(period)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        total = int(self.connection.execute(f"SELECT COUNT(*) FROM habitat_cases{where}", tuple(params)).fetchone()[0])
        rows = self.connection.execute(
            f"SELECT * FROM habitat_cases{where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"total": total, "data": [self._summary(row) for row in rows]}

    def detail(self, case_id: int) -> dict[str, Any]:
        case = self.connection.execute("SELECT * FROM habitat_cases WHERE id=?", (case_id,)).fetchone()
        if case is None:
            raise NotFoundError("栖息地案件不存在")
        result = self._summary(case)

        registrations = self.connection.execute(
            "SELECT * FROM habitat_registrations WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        result["registrations"] = [self._registration(row) for row in registrations]
        result["registration_count"] = len(registrations)

        decisions = self.connection.execute(
            "SELECT * FROM habitat_decisions WHERE case_id=? ORDER BY seq", (case_id,)
        ).fetchall()
        result["decisions"] = [self._decision(row) for row in decisions]

        followups = self.connection.execute(
            "SELECT * FROM habitat_followups WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        result["followups"] = [dict(row) for row in followups]

        emergencies = self.connection.execute(
            "SELECT * FROM habitat_emergencies WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        result["emergencies"] = [dict(row) for row in emergencies]
        if result["decisions"]:
            result["current_decision"] = result["decisions"][-1]
        return result

    # ---- helpers ----

    def _require_case(self, connection: sqlite3.Connection, case_id: int) -> sqlite3.Row:
        case = connection.execute("SELECT * FROM habitat_cases WHERE id=?", (case_id,)).fetchone()
        if case is None:
            raise NotFoundError("栖息地案件不存在")
        return case

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item.pop("location_key", None)
        return item

    @staticmethod
    def _registration(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["observed_species"] = json.loads(item.pop("observed_species_json") or "[]")
        return item

    @staticmethod
    def _decision(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["eco_basis"] = json.loads(item.pop("eco_basis_json") or "{}")
        return item
