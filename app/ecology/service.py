from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.ecology.schemas import DecisionValue, PileType, ReviewConclusion, RiskLevel
from app.services.audit import AuditContext, AuditService


SCHEMA = """
CREATE TABLE IF NOT EXISTS eco_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no TEXT UNIQUE,
    site_name TEXT NOT NULL,
    location_key TEXT NOT NULL,
    location_text TEXT NOT NULL,
    pile_type TEXT NOT NULL DEFAULT '其他',
    latitude REAL,
    longitude REAL,
    photo_summary TEXT NOT NULL DEFAULT '',
    season_risk TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT '中',
    period TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '待决定'
        CHECK(status IN ('待决定','保留中','调整中','已移除','紧急处置待补录','已关闭')),
    current_decision TEXT,
    registered_by_user_id INTEGER REFERENCES users(id),
    registered_by TEXT NOT NULL DEFAULT '',
    occurred_at TEXT,
    merged_into INTEGER REFERENCES eco_cases(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_eco_cases_active_period
ON eco_cases(location_key, period)
WHERE merged_into IS NULL AND status != '已移除';
CREATE INDEX IF NOT EXISTS idx_eco_cases_status ON eco_cases(status, id);

CREATE TABLE IF NOT EXISTS eco_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES eco_cases(id) ON DELETE RESTRICT,
    source TEXT NOT NULL CHECK(source IN ('staff','complaint','emergency')),
    pile_type TEXT NOT NULL DEFAULT '其他',
    photo_summary TEXT NOT NULL DEFAULT '',
    season_risk TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT '中',
    note TEXT NOT NULL DEFAULT '',
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL DEFAULT '',
    is_merged INTEGER NOT NULL DEFAULT 0 CHECK(is_merged IN (0,1)),
    occurred_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eco_registrations_case ON eco_registrations(case_id, id);

CREATE TABLE IF NOT EXISTS eco_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES eco_cases(id) ON DELETE RESTRICT,
    content TEXT NOT NULL,
    reporter_name TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    photo_summary TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '公众投诉',
    is_merged INTEGER NOT NULL DEFAULT 0 CHECK(is_merged IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eco_feedback_case ON eco_feedback(case_id, id);

CREATE TABLE IF NOT EXISTS eco_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES eco_cases(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('保留','调整','移除')),
    ecological_basis TEXT NOT NULL,
    basis_refs_json TEXT NOT NULL DEFAULT '[]',
    measures TEXT NOT NULL DEFAULT '',
    review_due_at TEXT,
    decided_by_user_id INTEGER REFERENCES users(id),
    decided_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eco_decisions_case ON eco_decisions(case_id, id);

CREATE TRIGGER IF NOT EXISTS trg_eco_decisions_no_update
BEFORE UPDATE ON eco_decisions
BEGIN
    SELECT RAISE(ABORT, '决定记录为追加台账，禁止修改');
END;
CREATE TRIGGER IF NOT EXISTS trg_eco_decisions_no_delete
BEFORE DELETE ON eco_decisions
BEGIN
    SELECT RAISE(ABORT, '决定记录为追加台账，禁止删除');
END;

CREATE TABLE IF NOT EXISTS eco_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES eco_cases(id) ON DELETE RESTRICT,
    seq INTEGER NOT NULL,
    conclusion_type TEXT NOT NULL
        CHECK(conclusion_type IN ('维持保留','建议调整','建议移除','继续观察')),
    finding TEXT NOT NULL,
    photo_summary TEXT NOT NULL DEFAULT '',
    next_review_at TEXT,
    reviewed_at TEXT NOT NULL,
    reviewer_user_id INTEGER REFERENCES users(id),
    reviewer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_eco_reviews_case ON eco_reviews(case_id, seq);

CREATE TRIGGER IF NOT EXISTS trg_eco_reviews_no_update
BEFORE UPDATE ON eco_reviews
BEGIN
    SELECT RAISE(ABORT, '复查结论只能新增，禁止覆盖旧记录');
END;
CREATE TRIGGER IF NOT EXISTS trg_eco_reviews_no_delete
BEFORE DELETE ON eco_reviews
BEGIN
    SELECT RAISE(ABORT, '复查结论只能新增，禁止删除');
END;

CREATE TABLE IF NOT EXISTS eco_emergencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES eco_cases(id) ON DELETE RESTRICT,
    action_taken TEXT NOT NULL,
    reason TEXT NOT NULL,
    hazard_type TEXT NOT NULL DEFAULT '',
    executed_at TEXT NOT NULL,
    supplement_deadline TEXT NOT NULL,
    responsible_person TEXT NOT NULL,
    supplement_status TEXT NOT NULL DEFAULT '待补录'
        CHECK(supplement_status IN ('待补录','已补录','已逾期补录')),
    supplemented_at TEXT,
    recorded_by_user_id INTEGER REFERENCES users(id),
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eco_emergencies_case ON eco_emergencies(case_id, id);
CREATE INDEX IF NOT EXISTS idx_eco_emergencies_supplement ON eco_emergencies(supplement_status, supplement_deadline);
"""

DECISION_STATUS = {
    DecisionValue.KEEP: "保留中",
    DecisionValue.ADJUST: "调整中",
    DecisionValue.REMOVE: "已移除",
}

_WHITESPACE = re.compile(r"\s+")


def ensure_schema() -> None:
    from app.database import get_connection

    get_connection().executescript(SCHEMA)


def normalize_location(site_name: str, location_text: str) -> str:
    joined = f"{site_name.strip()}|{location_text.strip()}".casefold()
    return _WHITESPACE.sub("", joined)


def period_of(value: datetime) -> str:
    """同一周期：11 月至次年 2 月归为同一个越冬周期，其余按自然年。"""
    year = value.year
    if value.month in (11, 12):
        return f"{year}-{year + 1}越冬周期"
    if value.month in (1, 2):
        return f"{year - 1}-{year}越冬周期"
    return f"{year}年常规周期"


def parse_datetime(raw: str | None, clock: Clock) -> datetime:
    if not raw:
        return clock.now()
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"无法解析时间：{raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class EcologyService:
    """落叶/枝堆生态保留案件台账服务。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)
        self.supplement_hours = int(os.getenv("ECO_SUPPLEMENT_HOURS", "48"))

    # ------------------------------------------------------------------ register

    def register(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        self.require_perm(principal, "eco.register", "eco.register.denied")
        occurred = parse_datetime(data.get("happened_at"), self.clock)
        self._validate_pile(data.get("pile_type"))
        self._validate_risk(data.get("risk_level", RiskLevel.MEDIUM))
        location_key = normalize_location(data["site_name"], data["location_text"])
        period = period_of(occurred)
        now = to_storage(self.clock.now())
        existing = self._find_open_case(location_key, period)
        if existing is not None:
            self.connection.execute(
                "INSERT INTO eco_registrations(case_id,source,pile_type,photo_summary,season_risk,risk_level,note,"
                "actor_user_id,actor_name,is_merged,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
                (
                    existing["id"], "staff", data["pile_type"], data.get("photo_summary", ""),
                    data.get("season_risk", ""), data.get("risk_level", "中"), data.get("note", ""),
                    principal.user_id, principal.display_name, to_storage(occurred), now,
                ),
            )
            self.connection.execute(
                "UPDATE eco_cases SET updated_at=? WHERE id=?", (now, existing["id"])
            )
            self._audit(principal, "eco.register.merged", existing["id"], after={"period": period})
            return self.detail(existing["id"], merged=True)

        cursor = self.connection.execute(
            "INSERT INTO eco_cases(site_name,location_key,location_text,pile_type,latitude,longitude,photo_summary,"
            "season_risk,risk_level,period,status,registered_by_user_id,registered_by,occurred_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?, '待决定', ?,?,?,?,?)",
            (
                data["site_name"].strip(), location_key, data["location_text"].strip(), data["pile_type"],
                data.get("latitude"), data.get("longitude"), data.get("photo_summary", ""),
                data.get("season_risk", ""), data.get("risk_level", "中"), period,
                principal.user_id, principal.display_name, to_storage(occurred), now, now,
            ),
        )
        case_id = int(cursor.lastrowid)
        case_no = f"ECO-{occurred.strftime('%Y%m%d')}-{case_id:05d}"
        self.connection.execute("UPDATE eco_cases SET case_no=? WHERE id=?", (case_no, case_id))
        self.connection.execute(
            "INSERT INTO eco_registrations(case_id,source,pile_type,photo_summary,season_risk,risk_level,note,"
            "actor_user_id,actor_name,is_merged,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
            (
                case_id, "staff", data["pile_type"], data.get("photo_summary", ""),
                data.get("season_risk", ""), data.get("risk_level", "中"), data.get("note", ""),
                principal.user_id, principal.display_name, to_storage(occurred), now,
            ),
        )
        self._audit(principal, "eco.register", case_id, after={"case_no": case_no, "period": period})
        return self.detail(case_id)

    def add_complaint(self, data: dict[str, Any]) -> dict[str, Any]:
        """公众投诉（无登录）：并入同地点同周期案件，没有则先建案件待工作人员补充。"""
        occurred = parse_datetime(data.get("happened_at"), self.clock)
        pile_type = data.get("pile_type") or PileType.OTHER
        self._validate_pile(pile_type)
        location_key = normalize_location(data["site_name"], data["location_text"])
        period = period_of(occurred)
        now = to_storage(self.clock.now())
        reporter = data.get("reporter_name", "") or "公众"
        existing = self._find_open_case(location_key, period)
        if existing is not None:
            cursor = self.connection.execute(
                "INSERT INTO eco_feedback(case_id,content,reporter_name,contact,photo_summary,is_merged,created_at) "
                "VALUES(?,?,?,?,?,1,?)",
                (
                    existing["id"], data["content"], data.get("reporter_name", ""), data.get("contact", ""),
                    data.get("photo_summary", ""), now,
                ),
            )
            self.connection.execute("UPDATE eco_cases SET updated_at=? WHERE id=?", (now, existing["id"]))
            self.audit.record(
                AuditContext(None, reporter),
                action="eco.complaint.merged",
                resource_type="eco_case",
                resource_id=existing["id"],
                after={"feedback_id": cursor.lastrowid, "period": period},
            )
            return self.detail(existing["id"], merged=True)

        cursor = self.connection.execute(
            "INSERT INTO eco_cases(site_name,location_key,location_text,pile_type,photo_summary,period,status,"
            "registered_by,occurred_at,created_at,updated_at) VALUES(?,?,?,?,?,?,'待决定',?,?,?,?)",
            (
                data["site_name"].strip(), location_key, data["location_text"].strip(), pile_type,
                data.get("photo_summary", ""), period, reporter, to_storage(occurred), now, now,
            ),
        )
        case_id = int(cursor.lastrowid)
        case_no = f"ECO-{occurred.strftime('%Y%m%d')}-{case_id:05d}"
        self.connection.execute("UPDATE eco_cases SET case_no=? WHERE id=?", (case_no, case_id))
        self.connection.execute(
            "INSERT INTO eco_registrations(case_id,source,pile_type,photo_summary,actor_name,occurred_at,created_at) "
            "VALUES(?, 'complaint', ?,?,?,?,?)",
            (case_id, pile_type, data.get("photo_summary", ""), reporter, to_storage(occurred), now),
        )
        self.connection.execute(
            "INSERT INTO eco_feedback(case_id,content,reporter_name,contact,photo_summary,is_merged,created_at) "
            "VALUES(?,?,?,?,?,0,?)",
            (
                case_id, data["content"], data.get("reporter_name", ""), data.get("contact", ""),
                data.get("photo_summary", ""), now,
            ),
        )
        self.audit.record(
            AuditContext(None, reporter),
            action="eco.complaint",
            resource_type="eco_case",
            resource_id=case_id,
            after={"case_no": case_no, "period": period},
        )
        return self.detail(case_id)

    # ------------------------------------------------------------------ decision

    def decide(self, principal: Principal, case_id: int, data: dict[str, Any]) -> dict[str, Any]:
        self.require_perm(principal, "eco.decide", "eco.decide.denied", resource_id=case_id)
        case = self._require_case(case_id)
        decision = data["decision"]
        if decision not in DecisionValue.ALL:
            raise ValidationError("决定必须是 保留/调整/移除 之一")
        if case["status"] == "紧急处置待补录":
            raise ConflictError("紧急处置案件尚未完成补录，不能出具保留决定")
        if decision == DecisionValue.ADJUST and not data.get("measures", "").strip():
            raise ValidationError("决定为“调整”时必须填写具体调整措施")
        now = to_storage(self.clock.now())
        review_due = to_storage(parse_datetime(data.get("review_due_at"), self.clock)) if data.get("review_due_at") else None
        before_status = case["status"]
        cursor = self.connection.execute(
            "INSERT INTO eco_decisions(case_id,decision,ecological_basis,basis_refs_json,measures,review_due_at,"
            "decided_by_user_id,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                case_id, decision, data["ecological_basis"],
                _json(data.get("basis_refs") or []), data.get("measures", ""), review_due,
                principal.user_id, principal.display_name, now,
            ),
        )
        new_status = DECISION_STATUS[decision]
        self.connection.execute(
            "UPDATE eco_cases SET status=?,current_decision=?,updated_at=? WHERE id=?",
            (new_status, decision, now, case_id),
        )
        self._audit(
            principal, "eco.decide", case_id,
            before={"status": before_status},
            after={"status": new_status, "decision": decision, "decision_id": cursor.lastrowid},
        )
        return self.detail(case_id)

    # ------------------------------------------------------------------ review

    def add_review(self, principal: Principal, case_id: int, data: dict[str, Any]) -> dict[str, Any]:
        self.require_perm(principal, "eco.review", "eco.review.denied", resource_id=case_id)
        case = self._require_case(case_id)
        conclusion = data["conclusion_type"]
        if conclusion not in ReviewConclusion.ALL:
            raise ValidationError("复查结论必须是 维持保留/建议调整/建议移除/继续观察 之一")
        reviewed_at = to_storage(parse_datetime(data.get("reviewed_at"), self.clock))
        next_review = to_storage(parse_datetime(data["next_review_at"], self.clock)) if data.get("next_review_at") else None
        now = to_storage(self.clock.now())
        last_seq = self.connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM eco_reviews WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        seq = int(last_seq) + 1
        cursor = self.connection.execute(
            "INSERT INTO eco_reviews(case_id,seq,conclusion_type,finding,photo_summary,next_review_at,"
            "reviewed_at,reviewer_user_id,reviewer,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                case_id, seq, conclusion, data["finding"], data.get("photo_summary", ""), next_review,
                reviewed_at, principal.user_id, principal.display_name, now,
            ),
        )
        # 复查只追加结论，不覆盖既有决定；案件时间戳更新，但状态/历史决定保持不变。
        self.connection.execute("UPDATE eco_cases SET updated_at=? WHERE id=?", (now, case_id))
        self._audit(
            principal, "eco.review.append", case_id,
            after={"seq": seq, "conclusion": conclusion, "review_id": cursor.lastrowid,
                   "previous_status": case["status"]},
        )
        return self.detail(case_id)

    # ------------------------------------------------------------------ emergency

    def record_emergency(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        self.require_perm(principal, "eco.emergency", "eco.emergency.denied")
        executed_at = parse_datetime(data.get("happened_at"), self.clock)
        self._validate_pile(data.get("pile_type", PileType.OTHER))
        location_key = normalize_location(data["site_name"], data["location_text"])
        period = period_of(executed_at)
        deadline = parse_datetime(data.get("supplement_deadline"), self.clock) if data.get("supplement_deadline") \
            else executed_at + timedelta(hours=self.supplement_hours)
        if deadline <= executed_at:
            raise ValidationError("补录期限必须晚于处置时间")
        now = to_storage(self.clock.now())
        existing = self._find_open_case(location_key, period)
        if existing is not None:
            case_id = existing["id"]
            merged = True
        else:
            cursor = self.connection.execute(
                "INSERT INTO eco_cases(site_name,location_key,location_text,pile_type,photo_summary,period,status,"
                "registered_by_user_id,registered_by,occurred_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'紧急处置待补录',?,?,?,?,?)",
                (
                    data["site_name"].strip(), location_key, data["location_text"].strip(),
                    data.get("pile_type", PileType.OTHER), data.get("photo_summary", ""), period,
                    principal.user_id, principal.display_name, to_storage(executed_at), now, now,
                ),
            )
            case_id = int(cursor.lastrowid)
            case_no = f"ECO-{executed_at.strftime('%Y%m%d')}-{case_id:05d}"
            self.connection.execute("UPDATE eco_cases SET case_no=? WHERE id=?", (case_no, case_id))
            merged = False
        emergency_cursor = self.connection.execute(
            "INSERT INTO eco_emergencies(case_id,action_taken,reason,hazard_type,executed_at,supplement_deadline,"
            "responsible_person,recorded_by_user_id,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                case_id, data["action_taken"], data["reason"], data.get("hazard_type", ""),
                to_storage(executed_at), to_storage(deadline), data["responsible_person"],
                principal.user_id, principal.display_name, now,
            ),
        )
        self.connection.execute(
            "INSERT INTO eco_registrations(case_id,source,pile_type,photo_summary,note,actor_user_id,actor_name,"
            "is_merged,occurred_at,created_at) VALUES(?, 'emergency', ?,?,?,?, ?, ?, ?,?)",
            (
                case_id, data.get("pile_type", PileType.OTHER), data.get("photo_summary", ""), data.get("note", ""),
                principal.user_id, principal.display_name, 1 if merged else 0, to_storage(executed_at), now,
            ),
        )
        self.connection.execute(
            "UPDATE eco_cases SET status='紧急处置待补录',updated_at=? WHERE id=?", (now, case_id)
        )
        self._audit(
            principal, "eco.emergency" if not merged else "eco.emergency.merged", case_id,
            after={
                "emergency_id": emergency_cursor.lastrowid,
                "executed_at": to_storage(executed_at),
                "supplement_deadline": to_storage(deadline),
                "responsible_person": data["responsible_person"],
            },
        )
        return self.detail(case_id, merged=merged)

    def supplement(self, principal: Principal, case_id: int, data: dict[str, Any]) -> dict[str, Any]:
        self.require_perm(principal, "eco.emergency", "eco.emergency.supplement.denied", resource_id=case_id)
        case = self._require_case(case_id)
        if case["status"] != "紧急处置待补录":
            raise ConflictError("该案件没有待补录的紧急处置记录")
        emergency = self.connection.execute(
            "SELECT * FROM eco_emergencies WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,)
        ).fetchone()
        if emergency is None or emergency["supplement_status"] != "待补录":
            raise ConflictError("紧急处置记录已完成补录，不能重复补录")
        self._validate_pile(data.get("pile_type"))
        self._validate_risk(data.get("risk_level", RiskLevel.MEDIUM))
        occurred = parse_datetime(data.get("happened_at"), self.clock)
        now = self.clock.now()
        late = to_storage(now) > emergency["supplement_deadline"]
        supplement_status = "已逾期补录" if late else "已补录"
        self.connection.execute(
            "UPDATE eco_emergencies SET supplement_status=?,supplemented_at=? WHERE id=?",
            (supplement_status, to_storage(now), emergency["id"]),
        )
        self.connection.execute(
            "UPDATE eco_cases SET pile_type=?,photo_summary=?,season_risk=?,risk_level=?,latitude=?,longitude=?,"
            "occurred_at=COALESCE(occurred_at,?),status='待决定',updated_at=? WHERE id=?",
            (
                data["pile_type"], data.get("photo_summary", ""), data.get("season_risk", ""),
                data.get("risk_level", "中"), data.get("latitude"), data.get("longitude"),
                to_storage(occurred), to_storage(now), case_id,
            ),
        )
        self.connection.execute(
            "INSERT INTO eco_registrations(case_id,source,pile_type,photo_summary,season_risk,risk_level,note,"
            "actor_user_id,actor_name,occurred_at,created_at) "
            "VALUES(?, 'staff', ?,?,?,?,?, ?,?,?,?)",
            (
                case_id, data["pile_type"], data.get("photo_summary", ""), data.get("season_risk", ""),
                data.get("risk_level", "中"), data.get("note", ""), principal.user_id,
                principal.display_name, to_storage(occurred), to_storage(now),
            ),
        )
        self._audit(
            principal, "eco.emergency.supplement", case_id,
            before={"supplement_status": "待补录"},
            after={"supplement_status": supplement_status, "late": late,
                   "deadline": emergency["supplement_deadline"]},
        )
        return self.detail(case_id)

    # ------------------------------------------------------------------ queries

    def list_cases(self, *, status: str | None = None, site_name: str | None = None,
                   period: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        conditions = ["merged_into IS NULL"]
        params: list[Any] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if site_name:
            conditions.append("site_name LIKE ?")
            params.append(f"%{site_name}%")
        if period:
            conditions.append("period=?")
            params.append(period)
        where = " WHERE " + " AND ".join(conditions)
        total = int(self.connection.execute(f"SELECT COUNT(*) FROM eco_cases{where}", tuple(params)).fetchone()[0])
        rows = self.connection.execute(
            f"SELECT * FROM eco_cases{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"total": total, "data": [self._decorate(dict(row)) for row in rows]}

    def detail(self, case_id: int, *, merged: bool = False) -> dict[str, Any]:
        case = self.connection.execute(
            "SELECT * FROM eco_cases WHERE id=?", (case_id,)
        ).fetchone()
        if case is None:
            raise NotFoundError("生态保留案件不存在")
        result = dict(case)
        result["merged"] = merged
        result["registrations"] = [dict(row) for row in self.connection.execute(
            "SELECT * FROM eco_registrations WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()]
        result["feedback"] = [dict(row) for row in self.connection.execute(
            "SELECT * FROM eco_feedback WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()]
        result["decisions"] = [
            {**dict(row), "basis_refs": _loads(row["basis_refs_json"])}
            for row in self.connection.execute(
                "SELECT * FROM eco_decisions WHERE case_id=? ORDER BY id", (case_id,)
            ).fetchall()
        ]
        result["reviews"] = [dict(row) for row in self.connection.execute(
            "SELECT * FROM eco_reviews WHERE case_id=? ORDER BY seq", (case_id,)
        ).fetchall()]
        result["emergencies"] = [dict(row) for row in self.connection.execute(
            "SELECT * FROM eco_emergencies WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()]
        return self._decorate(result)

    def overdue_supplements(self) -> list[dict[str, Any]]:
        now = to_storage(self.clock.now())
        rows = self.connection.execute(
            "SELECT e.*, c.case_no, c.site_name FROM eco_emergencies e "
            "JOIN eco_cases c ON c.id=e.case_id "
            "WHERE e.supplement_status='待补录' AND e.supplement_deadline < ? ORDER BY e.supplement_deadline",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ helpers

    def require_perm(self, principal: Principal, permission: str, action: str,
                     resource_id: int | None = None, details: dict | None = None) -> None:
        """权限校验并记录拒绝审计。必须在业务事务开启前调用，拒绝时不产生任何业务写入。"""
        if principal.can(permission):
            return
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action=action,
            resource_type="eco_case",
            resource_id=resource_id,
            outcome="denied",
            metadata={"required_permission": permission, **(details or {})},
        )
        raise PermissionDeniedError(f"缺少权限：{permission}")

    def _decorate(self, case: dict[str, Any]) -> dict[str, Any]:
        case["supplement_overdue"] = any(
            item.get("supplement_status") == "待补录" and item.get("supplement_deadline") and item["supplement_deadline"] < to_storage(self.clock.now())
            for item in case.get("emergencies", [])
        )
        return case

    def _find_open_case(self, location_key: str, period: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM eco_cases WHERE location_key=? AND period=? AND merged_into IS NULL "
            "AND status != '已移除' ORDER BY id DESC LIMIT 1",
            (location_key, period),
        ).fetchone()

    def _require_case(self, case_id: int) -> sqlite3.Row:
        case = self.connection.execute(
            "SELECT * FROM eco_cases WHERE id=?", (case_id,)
        ).fetchone()
        if case is None:
            raise NotFoundError("生态保留案件不存在")
        return case

    def _audit(self, principal: Principal, action: str, case_id: int, *,
               before: dict | None = None, after: dict | None = None) -> None:
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action=action,
            resource_type="eco_case",
            resource_id=case_id,
            before=before,
            after=after,
        )

    @staticmethod
    def _validate_pile(value: str | None) -> None:
        if value not in PileType.ALL:
            raise ValidationError("堆积类型必须是 落叶/枝堆/混合堆积/其他")

    @staticmethod
    def _validate_risk(value: str | None) -> None:
        if value not in RiskLevel.ALL:
            raise ValidationError("风险等级必须是 低/中/高")


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _loads(raw: str) -> Any:
    import json

    try:
        return json.loads(raw or "[]")
    except ValueError:
        return []
