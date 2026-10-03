#!/usr/bin/env python3
"""Drone flight-plan approval and airspace coordination service (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PORT = 8205
ROLES = {"viewer", "operator", "airspace_reviewer", "commander", "auditor"}
ACTIVE_STATUSES = {"submitted", "approved"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message); self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def route_bbox(route: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in route]; ys = [float(point[1]) for point in route]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], buffer: float = 0.0) -> bool:
    return a[0] <= b[2] + buffer and a[2] + buffer >= b[0] and a[1] <= b[3] + buffer and a[3] + buffer >= b[1]


def times_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool: return a_start < b_end and b_start < a_end


def validate_route(route: Any) -> list[list[float]]:
    if not isinstance(route, list) or len(route) < 2: raise ApiError(400, "invalid_route", "航线至少需要两个经纬度点")
    normalized: list[list[float]] = []
    for point in route:
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) for v in point): raise ApiError(400, "invalid_route_point", "每个航线点必须是 [经度,纬度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90: raise ApiError(400, "invalid_coordinates", "经纬度超出范围")
        normalized.append([lon, lat])
    return normalized


class Repository:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.local = threading.local()
        self._init(self.connect())

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row; conn.execute("PRAGMA foreign_keys=ON"); conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        current = getattr(self.local, "conn", None)
        if current is None:
            current = self.connect(); self.local.conn = current
        return current
    def _init(self, init_conn: sqlite3.Connection) -> None:
        init_conn.execute("PRAGMA journal_mode=WAL")
        init_conn.executescript("""
        CREATE TABLE IF NOT EXISTS restrictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flight_plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, callsign TEXT NOT NULL, drone_model TEXT NOT NULL,
            payload_kg REAL NOT NULL, route_json TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, max_altitude REAL NOT NULL,
            population_risk INTEGER NOT NULL, emergency_plan TEXT NOT NULL, region TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(operator_id,callsign,starts_at)
        );
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, offline_id TEXT UNIQUE,
            override_kind TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, operator_id TEXT NOT NULL,
            leader_plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            status TEXT NOT NULL DEFAULT 'forming', revision INTEGER NOT NULL DEFAULT 1,
            emergency INTEGER NOT NULL DEFAULT 0, displaced_by_formation_id INTEGER, displaced_at TEXT,
            basis_approval_id INTEGER, last_approved_at TEXT,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formation_members(
            formation_id INTEGER NOT NULL REFERENCES formations(id), plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            role TEXT NOT NULL DEFAULT 'member', snapshot_revision INTEGER, joined_at TEXT NOT NULL,
            PRIMARY KEY(formation_id,plan_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_plan_unique ON formation_members(plan_id);
        CREATE TABLE IF NOT EXISTS formation_approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, formation_id INTEGER NOT NULL REFERENCES formations(id),
            formation_revision INTEGER NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
            offline_id TEXT UNIQUE, override_kind TEXT, basis_approval_id INTEGER, preempted_for_formation_id INTEGER,
            member_snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS capacity_windows(
            id INTEGER PRIMARY KEY AUTOINCREMENT, region TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
            slots INTEGER NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formation_slot_occupancy(
            id INTEGER PRIMARY KEY AUTOINCREMENT, window_id INTEGER NOT NULL REFERENCES capacity_windows(id),
            formation_approval_id INTEGER NOT NULL REFERENCES formation_approvals(id),
            formation_id INTEGER NOT NULL REFERENCES formations(id), plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            status TEXT NOT NULL DEFAULT 'held', held_at TEXT NOT NULL, released_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_occ_window ON formation_slot_occupancy(window_id,status);
        """)
        approval_cols = {row["name"] for row in init_conn.execute("PRAGMA table_info(approvals)")}
        if "formation_approval_id" not in approval_cols:
            init_conn.execute("ALTER TABLE approvals ADD COLUMN formation_approval_id INTEGER")
        init_conn.close()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try: yield self.conn; self.conn.execute("COMMIT")
        except Exception: self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)", (plan_id, kind, message, iso()))


class DroneAirspaceService:
    def __init__(self, path: str | Path): self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def create_restriction(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        name, kind, reason = str(body.get("name", "")).strip(), str(body.get("kind", "")).strip(), str(body.get("reason", "")).strip()
        if kind not in {"no_fly", "temporary_limit"} or not name or not reason: raise ApiError(400, "invalid_restriction", "名称、类型和原因必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_restriction", "空域范围和高度必须为数字")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or end <= start:
            raise ApiError(400, "invalid_restriction", "空域范围、高度或时间无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO restrictions(name,kind,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,starts_at,ends_at,reason,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, iso()))
            return dict(conn.execute("SELECT * FROM restrictions WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_plan(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "plan_forbidden", "只有运营方可以创建飞行计划")
        required = ("callsign", "drone_model", "starts_at", "ends_at", "emergency_plan", "region")
        if any(body.get(key) in (None, "") for key in required): raise ApiError(400, "missing_fields", "飞行计划字段不完整")
        route = validate_route(body.get("route")); start, end = parse_time(body["starts_at"]), parse_time(body["ends_at"])
        try: payload, altitude = float(body.get("payload_kg")), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_numbers", "payload_kg 和 max_altitude 必须为数字")
        risk = body.get("population_risk")
        if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5:
            raise ApiError(400, "invalid_plan", "载荷、高度或人口风险无效")
        if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "飞行时间必须在未来且结束晚于开始")
        bbox = route_bbox(route)
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("""INSERT INTO flight_plans(operator_id,callsign,drone_model,payload_kg,route_json,starts_at,ends_at,max_altitude,population_risk,emergency_plan,region,created_by,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (operator, str(body["callsign"]).upper(), body["drone_model"], payload, json.dumps(route), iso(start), iso(end), altitude, risk, body["emergency_plan"], body["region"], actor, iso(), iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "plan_duplicate", "同一运营方、呼号和起飞时间的计划已存在") from exc
            plan_id = cur.lastrowid; Repository.audit(conn, plan_id, actor, role, "plan_created", {"bbox": bbox, "revision": 1})
            return self.get_plan(plan_id, role, operator)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        route = self._route(plan); bbox = route_bbox(route); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            altitude_overlap = plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0
            if altitude_overlap:
                item = {"code": "airspace_restriction", "restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]}
                blocking.append(item)
        adjacent: list[dict[str, Any]] = []
        for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
            if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002):
                adjacent.append({"plan_id": other["id"], "callsign": other["callsign"], "operator_id": other["operator_id"], "status": other["status"], "starts_at": other["starts_at"], "ends_at": other["ends_at"]})
        if adjacent: blocking.append({"code": "adjacent_traffic", "plans": adjacent, "message": "相邻航路与有效计划重叠"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "hard_violations": hard, "blocking_conflicts": blocking, "approvable": not hard and not blocking}

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "valid_until" if "valid_until" in result else "updated_at")}
        if role in {"airspace_reviewer", "commander", "auditor"}: result["approvals"] = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
        return result

    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            if plan["status"] == "submitted": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "approved":
                    return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] == "approved": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以批准")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，审核决定不能套用")
            report = self._conflict_report(conn, plan)
            if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束", report)
            if report["blocking_conflicts"] and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
            override_kind = "emergency_authority" if report["blocking_conflicts"] else None
            cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,created_at)
                                  VALUES(?,?,?,?,?,?,?,?)""", (plan_id, expected, actor, "approved", reason, offline_id, override_kind, iso()))
            conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
            if override_kind: Repository.audit(conn, plan_id, actor, role, "emergency_override_used", {"override_reason": override, "conflicts": report["blocking_conflicts"]})
            Repository.audit(conn, plan_id, actor, role, "plan_approved", {"revision": expected, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "approved", f"飞行计划 {plan['callsign']} 已批准")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False, "approval_id": cur.lastrowid, "override_kind": override_kind}

    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "当前角色不能拒绝计划")
        expected, offline_id, reason = body.get("expected_revision"), str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "rejected": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经被使用")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "submitted" or plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划状态或版本不匹配")
            conn.execute("INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, expected, actor, "rejected", reason, offline_id, iso()))
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_rejected", {"reason": reason, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False}

    def change(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "change_forbidden", "只有运营方可以变更计划")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能修改其他运营方计划")
            if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", "已取消或过期计划不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化")
            route = validate_route(body.get("route", self._route(plan)))
            start = parse_time(body.get("starts_at", plan["starts_at"])); end = parse_time(body.get("ends_at", plan["ends_at"]))
            if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "新飞行时间无效")
            payload = float(body.get("payload_kg", plan["payload_kg"])); altitude = float(body.get("max_altitude", plan["max_altitude"]))
            risk = body.get("population_risk", plan["population_risk"])
            if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5: raise ApiError(400, "invalid_plan", "变更后的载荷、高度或风险无效")
            revision = expected + 1
            team_member = conn.execute("SELECT formation_id FROM formation_members WHERE plan_id=?", (plan_id,)).fetchone()
            team_status = conn.execute("SELECT status FROM formations WHERE id=?", (team_member["formation_id"],)).fetchone()["status"] if team_member else None
            invalidate_team = team_status == "approved"
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), revision, iso(), plan_id))
            if invalidate_team:
                self._invalidate_formation_approval(conn, team_member["formation_id"], actor, role, "formation_member_changed", {"plan_id": plan_id, "plan_revision": revision})
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"], "formation_id": team_member["formation_id"] if team_member else None, "team_approval_invalidated": invalidate_team})
            if invalidate_team:
                Repository.notify(conn, plan_id, "approval_invalidated", f"成员 {plan['callsign']} 改动触发编队整队批准作废并释放名额，需要重新整队审批")
            elif team_status == "displaced":
                Repository.notify(conn, plan_id, "displaced_member_changed", f"被顶出编队成员 {plan['callsign']} 已改动，原批准依据将在复队核对时失效")
            elif plan["status"] == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
            else: Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            return self.get_plan(plan_id, role, operator)

    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            if plan["status"] == "canceled": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] == "expired": raise ApiError(409, "plan_expired", "已过期计划不能取消")
            team_member = conn.execute("SELECT * FROM formation_members WHERE plan_id=?", (plan_id,)).fetchone()
            if team_member:
                formation = self._formation_row(conn, team_member["formation_id"])
                if formation["leader_plan_id"] == plan_id: raise ApiError(409, "leader_cannot_leave", "队长计划不能直接取消，请先解散编队或拆分更换队长")
                if formation["status"] == "approved":
                    self._invalidate_formation_approval(conn, formation["id"], actor, role, "formation_member_canceled", {"plan_id": plan_id, "reason": reason})
                conn.execute("DELETE FROM formation_members WHERE formation_id=? AND plan_id=?", (formation["id"], plan_id))
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason, "formation_id": team_member["formation_id"] if team_member else None})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    # ---------- formations ----------

    def _formation_row(self, conn: sqlite3.Connection, formation_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
        if not row: raise ApiError(404, "formation_not_found", "编队不存在")
        return row

    def _member_rows(self, conn: sqlite3.Connection, formation_id: int) -> list[sqlite3.Row]:
        return list(conn.execute("""SELECT fm.*,p.* FROM formation_members fm JOIN flight_plans p ON p.id=fm.plan_id
                                    WHERE fm.formation_id=? ORDER BY fm.plan_id""", (formation_id,)))

    def _snapshot(self, members: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [{"plan_id": m["id"], "revision": m["revision"]} for m in members]

    def _member_plans(self, conn: sqlite3.Connection, plan_ids: list[int]) -> list[sqlite3.Row]:
        if not plan_ids: return []
        marks = ",".join("?" for _ in plan_ids)
        return list(conn.execute(f"SELECT * FROM flight_plans WHERE id IN ({marks})", plan_ids))

    def formation_dict(self, conn: sqlite3.Connection, formation: sqlite3.Row, include_members: bool = True) -> dict[str, Any]:
        result = dict(formation)
        result["emergency"] = bool(result["emergency"])
        approval_id = formation["basis_approval_id"]
        if approval_id:
            basis = conn.execute("SELECT * FROM formation_approvals WHERE id=?", (approval_id,)).fetchone()
            result["basis_approval"] = dict(basis) if basis else None
        if include_members:
            members = []
            for m in self._member_rows(conn, formation["id"]):
                item = {"plan_id": m["plan_id"], "callsign": m["callsign"], "role": m["role"],
                        "snapshot_revision": m["snapshot_revision"], "current_revision": m["revision"],
                        "starts_at": m["starts_at"], "ends_at": m["ends_at"], "region": m["region"], "status": m["status"]}
                members.append(item)
            result["members"] = members; result["member_count"] = len(members)
        return result

    def create_formation(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以组建编队")
        name = str(body.get("name", "")).strip()
        if not name: raise ApiError(400, "missing_fields", "编队名称必填")
        plan_ids = body.get("plan_ids")
        if not isinstance(plan_ids, list) or not plan_ids or any(not isinstance(x, int) for x in plan_ids): raise ApiError(400, "member_required", "必须提供至少一个成员计划 plan_ids")
        leader = body.get("leader_plan_id")
        plan_ids = list(dict.fromkeys(plan_ids))
        if not isinstance(leader, int) or leader not in plan_ids: raise ApiError(400, "leader_required", "leader_plan_id 必须是成员之一")
        with self.repo.tx() as conn:
            plans = self._member_plans(conn, plan_ids)
            if len(plans) != len(plan_ids): raise ApiError(404, "plan_not_found", "部分飞行计划不存在")
            for plan in plans:
                if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "编队成员必须属于同一运营方")
                if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", f"计划 {plan['callsign']} 已取消或过期")
                bound = conn.execute("SELECT formation_id FROM formation_members WHERE plan_id=?", (plan["id"],)).fetchone()
                if bound: raise ApiError(409, "member_already_bound", f"计划 {plan['callsign']} 已属于编队 {bound['formation_id']}")
            now = iso()
            cur = conn.execute("""INSERT INTO formations(name,operator_id,leader_plan_id,created_by,created_at,updated_at)
                                  VALUES(?,?,?,?,?,?)""", (name, operator, leader, actor, now, now))
            fid = cur.lastrowid
            for plan in plans:
                conn.execute("INSERT INTO formation_members(formation_id,plan_id,role,joined_at) VALUES(?,?,?,?)",
                             (fid, plan["id"], "leader" if plan["id"] == leader else "member", now))
            Repository.audit(conn, None, actor, role, "formation_created", {"formation_id": fid, "members": plan_ids})
            return self.formation_dict(conn, self._formation_row(conn, fid))

    def add_member(self, formation_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以维护编队成员")
        plan_id = body.get("plan_id")
        if not isinstance(plan_id, int): raise ApiError(400, "plan_required", "plan_id 必填")
        leader = bool(body.get("as_leader"))
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            if formation["operator_id"] != operator: raise ApiError(403, "formation_forbidden", "不能维护其他运营方编队")
            if formation["status"] not in {"forming", "invalidated"}: raise ApiError(409, "formation_locked", "已提交/批准/顶出的编队请先拆分或申请复队")
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能绑定其他运营方计划")
            if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", "已取消或过期计划不能入队")
            if conn.execute("SELECT formation_id FROM formation_members WHERE plan_id=?", (plan_id,)).fetchone(): raise ApiError(409, "member_already_bound", "该计划已属于编队")
            conn.execute("INSERT INTO formation_members(formation_id,plan_id,role,joined_at) VALUES(?,?,?,?)", (formation_id, "leader" if leader else "member", iso()))
            if leader:
                conn.execute("UPDATE formation_members SET role='member' WHERE formation_id=? AND plan_id<>?", (formation_id, plan_id))
                conn.execute("UPDATE formations SET leader_plan_id=?,updated_at=? WHERE id=?", (plan_id, iso(), formation_id))
            Repository.audit(conn, None, actor, role, "formation_member_added", {"formation_id": formation_id, "plan_id": plan_id})
            return self.formation_dict(conn, self._formation_row(conn, formation_id))

    def remove_member(self, formation_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以维护编队成员")
        plan_id = body.get("plan_id")
        if not isinstance(plan_id, int): raise ApiError(400, "plan_required", "plan_id 必填")
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            if formation["operator_id"] != operator: raise ApiError(403, "formation_forbidden", "不能维护其他运营方编队")
            if formation["status"] not in {"forming", "invalidated"}: raise ApiError(409, "formation_locked", "已提交/批准/顶出的编队请使用拆分")
            member = conn.execute("SELECT * FROM formation_members WHERE formation_id=? AND plan_id=?", (formation_id, plan_id)).fetchone()
            if not member: raise ApiError(404, "member_not_found", "该计划不在编队中")
            remaining = conn.execute("SELECT COUNT(*) c FROM formation_members WHERE formation_id=?", (formation_id,)).fetchone()["c"] - 1
            if remaining < 1: raise ApiError(409, "formation_too_small", "编队至少保留一个成员；请使用解散接口")
            conn.execute("DELETE FROM formation_members WHERE formation_id=? AND plan_id=?", (formation_id, plan_id))
            if formation["leader_plan_id"] == plan_id:
                new_leader = conn.execute("SELECT plan_id FROM formation_members WHERE formation_id=? ORDER BY plan_id LIMIT 1", (formation_id,)).fetchone()
                conn.execute("UPDATE formation_members SET role='leader' WHERE formation_id=? AND plan_id=?", (formation_id, new_leader["plan_id"]))
                conn.execute("UPDATE formations SET leader_plan_id=?,updated_at=? WHERE id=?", (new_leader["plan_id"], iso(), formation_id))
            Repository.audit(conn, None, actor, role, "formation_member_removed", {"formation_id": formation_id, "plan_id": plan_id})
            return self.formation_dict(conn, self._formation_row(conn, formation_id))

    def submit_formation(self, formation_id: int, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以提交编队")
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            if formation["operator_id"] != operator: raise ApiError(403, "formation_forbidden", "不能提交其他运营方编队")
            if formation["status"] == "submitted": return {"formation": self.formation_dict(conn, formation), "idempotent": True}
            if formation["status"] not in {"forming", "invalidated"}: raise ApiError(409, "invalid_transition", "当前编队状态不能提交")
            members = self._member_rows(conn, formation_id)
            if len(members) < 1: raise ApiError(409, "formation_empty", "编队没有成员")
            for m in members:
                if parse_time(m["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", f"成员 {m['callsign']} 起飞时间已过")
                if m["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", f"成员 {m['callsign']} 已关闭")
            revision = formation["revision"] + 1
            now = iso()
            conn.execute("UPDATE formations SET status='submitted',revision=?,updated_at=? WHERE id=?", (revision, now, formation_id))
            for m in members:
                conn.execute("UPDATE formation_members SET snapshot_revision=? WHERE formation_id=? AND plan_id=?", (m["revision"], formation_id, m["plan_id"]))
                conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (now, m["plan_id"]))
            Repository.audit(conn, None, actor, role, "formation_submitted", {"formation_id": formation_id, "revision": revision})
            return {"formation": self.formation_dict(conn, self._formation_row(conn, formation_id)), "idempotent": False}

    def _covering_windows(self, conn: sqlite3.Connection, member: sqlite3.Row) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM capacity_windows WHERE region=? AND starts_at<? AND ends_at>? ORDER BY id",
                                 (member["region"], member["ends_at"], member["starts_at"])))

    def _formation_conflict_report(self, conn: sqlite3.Connection, formation: sqlite3.Row, members: list[sqlite3.Row]) -> dict[str, Any]:
        member_issues: list[dict[str, Any]] = []
        snapshots = {m["plan_id"]: conn.execute("SELECT snapshot_revision FROM formation_members WHERE formation_id=? AND plan_id=?", (formation["id"], m["plan_id"])).fetchone()["snapshot_revision"] for m in members}
        stale_members: list[int] = []
        plan_ids = {m["id"] for m in members}
        for m in members:
            report = self._conflict_report(conn, m)
            hard, blocking = report["hard_violations"], []
            for block in report["blocking_conflicts"]:
                if block.get("code") == "adjacent_traffic":
                    outside = [p for p in block["plans"] if p["plan_id"] not in plan_ids]
                    if not outside: continue
                    block = dict(block); block["plans"] = outside
                blocking.append(block)
            if hard or blocking: member_issues.append({"plan_id": m["id"], "callsign": m["callsign"], "hard_violations": hard, "blocking_conflicts": blocking})
            snap = snapshots.get(m["id"])
            if snap is not None and snap != m["revision"]: stale_members.append(m["id"])
        window_errors: list[dict[str, Any]] = []
        windows_by_id: dict[int, sqlite3.Row] = {}
        for m in members:
            covers = self._covering_windows(conn, m)
            if not covers: window_errors.append({"code": "capacity_window_missing", "plan_id": m["id"], "callsign": m["callsign"], "region": m["region"], "starts_at": m["starts_at"], "ends_at": m["ends_at"]})
            for w in covers: windows_by_id[w["id"]] = w
        capacity: list[dict[str, Any]] = []
        for wid, window in sorted(windows_by_id.items()):
            members_in_window = [m for m in members if window["region"] == m["region"] and parse_time(window["starts_at"]) < parse_time(m["ends_at"]) and parse_time(window["ends_at"]) > parse_time(m["starts_at"])]
            held = conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE window_id=? AND status='held'", (wid,)).fetchone()["c"]
            solo = conn.execute("""SELECT COUNT(DISTINCT p.id) c FROM flight_plans p
                                        WHERE p.id NOT IN (SELECT plan_id FROM formation_members) AND p.status='approved'
                                        AND p.region=? AND p.starts_at<? AND p.ends_at>?""",
                                     (window["region"], window["ends_at"], window["starts_at"])).fetchone()["c"]
            need = len(members_in_window); other = held + solo; remaining = window["slots"] - other
            conflicting: list[dict[str, Any]] = []
            if remaining < need:
                rows = conn.execute("""SELECT DISTINCT o.formation_id,o.formation_approval_id
                                       FROM formation_slot_occupancy o JOIN formations f ON f.id=o.formation_id
                                       WHERE o.window_id=? AND o.status='held'
                                       ORDER BY f.last_approved_at DESC, o.formation_id""", (wid,))
                for r in rows:
                    conflicting.append({"kind": "formation", "formation_id": r["formation_id"], "approval_id": r["formation_approval_id"]})
                for p in conn.execute("""SELECT p.id,p.callsign,p.operator_id FROM flight_plans p
                                       WHERE p.id NOT IN (SELECT plan_id FROM formation_members) AND p.status='approved'
                                       AND p.region=? AND p.starts_at<? AND p.ends_at>?""",
                                      (window["region"], window["ends_at"], window["starts_at"])):
                    conflicting.append({"kind": "solo_approved", "plan_id": p["id"], "callsign": p["callsign"], "operator_id": p["operator_id"]})
            capacity.append({"window_id": wid, "region": window["region"], "starts_at": window["starts_at"], "ends_at": window["ends_at"],
                            "slots": window["slots"], "held_by_formations": held, "solo_approved": solo,
                            "needed": need, "remaining": remaining,
                            "conflicting": conflicting if remaining < need else []})
        capacity_blocked = any(c["remaining"] < c["needed"] for c in capacity)
        return {"formation_id": formation["id"], "revision": formation["revision"], "status": formation["status"],
                "member_count": len(members), "member_issues": member_issues, "stale_members": stale_members,
                "capacity_window_errors": window_errors, "capacity": capacity,
                "approvable": not member_issues and not stale_members and not window_errors and not capacity_blocked}

    def check_formation(self, formation_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            if role == "operator" and formation["operator_id"] != operator: raise ApiError(403, "formation_forbidden", "不能查看其他运营方编队")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查编队冲突")
            return self._formation_conflict_report(conn, formation, self._member_rows(conn, formation_id))

    def _release_occupancy(self, conn: sqlite3.Connection, approval_id: int) -> None:
        conn.execute("UPDATE formation_slot_occupancy SET status='released',released_at=? WHERE formation_approval_id=? AND status='held'", (iso(), approval_id))

    def _hold_occupancy(self, conn: sqlite3.Connection, approval_id: int, formation_id: int, members: list[sqlite3.Row]) -> None:
        now = iso()
        for m in members:
            for w in self._covering_windows(conn, m):
                conn.execute("""INSERT INTO formation_slot_occupancy(window_id,formation_approval_id,formation_id,plan_id,held_at)
                                VALUES(?,?,?,?,?)""", (w["id"], approval_id, formation_id, m["plan_id"], now))

    def _invalidate_formation_approval(self, conn: sqlite3.Connection, formation_id: int, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        formation = self._formation_row(conn, formation_id)
        basis = formation["basis_approval_id"]
        if basis:
            self._release_occupancy(conn, basis)
            conn.execute("UPDATE formation_approvals SET preempted_for_formation_id=COALESCE(preempted_for_formation_id,?) WHERE id=?", (detail.get("preempted_for"), basis))
        now = iso()
        members = self._member_rows(conn, formation_id)
        for m in members:
            conn.execute("UPDATE flight_plans SET status='draft',updated_at=? WHERE id=? AND status IN ('submitted','approved','displaced')", (now, m["plan_id"]))
        conn.execute("UPDATE formations SET status='invalidated',basis_approval_id=NULL,displaced_by_formation_id=NULL,displaced_at=NULL,last_approved_at=NULL,updated_at=? WHERE id=?", (now, formation_id))
        for m in members:
            Repository.notify(conn, m["plan_id"], "formation_approval_invalidated", f"编队 {formation['name']} 的整队批准已作废，原占用名额已释放")
        Repository.audit(conn, None, actor, role, action, {"formation_id": formation_id, **detail})

    def approve_formation(self, formation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准编队")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        preempt = bool(body.get("preempt"))
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        if preempt and not (role == "commander" and override): raise ApiError(403, "preempt_forbidden", "救援队顶出需要指挥官提供 override_reason")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM formation_approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["formation_id"] == formation_id and prior["formation_revision"] == expected and prior["decision"] == "approved":
                    return {"formation": self.formation_dict(conn, self._formation_row(conn, formation_id)), "idempotent": True, "formation_approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            formation = self._formation_row(conn, formation_id)
            if formation["status"] == "approved": return {"formation": self.formation_dict(conn, formation), "idempotent": True}
            if formation["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交编队可以批准")
            if formation["revision"] != expected: raise ApiError(409, "revision_conflict", "编队版本已变化，请重新核对成员")
            members = self._member_rows(conn, formation_id)
            report = self._formation_conflict_report(conn, formation, members)
            if report["stale_members"]: raise ApiError(409, "formation_members_changed", "成员计划在提交后被改动，整队批准不能套用，请重新提交", {"stale_members": report["stale_members"]})
            if report["member_issues"]:
                hard = any(i["hard_violations"] for i in report["member_issues"])
                if hard or not (role == "commander" and override):
                    raise ApiError(409, "formation_member_conflict", "成员计划存在未解决的安全或空域冲突", report)
            if report["capacity_window_errors"]: raise ApiError(409, "capacity_window_missing", "编队活动时段没有配置空域名额窗口", report["capacity_window_errors"])
            capacity_blocked = any(c["remaining"] < c["needed"] for c in report["capacity"])
            displaced: list[int] = []
            if capacity_blocked:
                if not preempt: raise ApiError(409, "capacity_full", "空域名额不足，整队不能占用", report)
                displaced = self._preempt_capacity(conn, formation_id, members)
                if displaced is None:
                    raise ApiError(409, "preempt_possible", "顶出后仍无法腾出足够名额", report)
                formation = self._formation_row(conn, formation_id)
                report = self._formation_conflict_report(conn, formation, members)
                if any(c["remaining"] < c["needed"] for c in report["capacity"]): raise ApiError(409, "capacity_full", "顶出后仍存在容量冲突", report)
            override_kind = "rescue_preempt" if preempt else ("emergency_authority" if report["member_issues"] else None)
            snapshot = self._snapshot(members); now = iso()
            cur = conn.execute("""INSERT INTO formation_approvals(formation_id,formation_revision,reviewer,decision,reason,offline_id,override_kind,member_snapshot_json,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?)""", (formation_id, expected, actor, "approved", reason, offline_id, override_kind, json.dumps(snapshot, ensure_ascii=False), now))
            approval_id = cur.lastrowid
            self._hold_occupancy(conn, approval_id, formation_id, members)
            conn.execute("UPDATE formations SET status='approved',emergency=?,basis_approval_id=?,last_approved_at=?,updated_at=? WHERE id=?",
                         (1 if preempt else 0, approval_id, now, now, formation_id))
            for m in members:
                conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (now, m["plan_id"]))
                conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,formation_approval_id,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?)""", (m["plan_id"], m["revision"], actor, "approved", f"编队整队批准：{reason}", None, override_kind, approval_id, now))
                Repository.notify(conn, m["plan_id"], "formation_approved", f"编队 {formation['name']} 已整队放行，共占用 {len(members)} 个名额")
            for did in displaced:
                Repository.audit(conn, None, actor, role, "formation_preempted", {"formation_id": did, "preempted_by": formation_id, "approval_id": approval_id})
            Repository.audit(conn, None, actor, role, "formation_approved", {"formation_id": formation_id, "revision": expected, "offline_id": offline_id, "displaced": displaced, "override_kind": override_kind})
            return {"formation": self.formation_dict(conn, self._formation_row(conn, formation_id)), "idempotent": False, "formation_approval_id": approval_id, "override_kind": override_kind, "displaced_formation_ids": displaced}

    def _preempt_capacity(self, conn: sqlite3.Connection, formation_id: int, members: list[sqlite3.Row]) -> list[int] | None:
        displaced: list[int] = []
        guard = 0
        while True:
            guard += 1
            if guard > 100: return None
            blocked_windows = [w for w in self._current_capacity(conn, formation_id, members) if w["remaining"] < w["needed"]]
            if not blocked_windows: return displaced
            candidates = conn.execute("""SELECT DISTINCT o.formation_id FROM formation_slot_occupancy o
                                         JOIN formations f ON f.id=o.formation_id
                                         WHERE o.status='held' AND f.emergency=0 AND f.status='approved'
                                         AND o.window_id IN (%s) ORDER BY f.last_approved_at""" % ",".join(str(w["window_id"]) for w in blocked_windows))
            victim_id = None
            for row in candidates:
                if row["formation_id"] not in displaced: victim_id = row["formation_id"]; break
            if victim_id is None: return None
            self._displace_formation(conn, victim_id, preempted_by=formation_id)
            displaced.append(victim_id)

    def _current_capacity(self, conn: sqlite3.Connection, formation_id: int, members: list[sqlite3.Row]) -> list[dict[str, Any]]:
        formation = self._formation_row(conn, formation_id)
        report = self._formation_conflict_report(conn, formation, members)
        return report["capacity"]

    def _displace_formation(self, conn: sqlite3.Connection, victim_id: int, preempted_by: int) -> None:
        victim = self._formation_row(conn, victim_id); basis = victim["basis_approval_id"]; now = iso()
        if basis:
            self._release_occupancy(conn, basis)
            conn.execute("UPDATE formation_approvals SET preempted_for_formation_id=? WHERE id=?", (preempted_by, basis))
        for m in self._member_rows(conn, victim_id):
            conn.execute("UPDATE flight_plans SET status='displaced',updated_at=? WHERE id=?", (now, m["plan_id"]))
            Repository.notify(conn, m["plan_id"], "formation_displaced", f"编队 {victim['name']} 被救援队顶出，原批准依据保留，可申请复队")
        conn.execute("UPDATE formations SET status='displaced',displaced_by_formation_id=?,displaced_at=?,updated_at=? WHERE id=?", (preempted_by, now, now, victim_id))
        Repository.notify(conn, victim["leader_plan_id"], "formation_displaced", f"编队 {victim['name']} 被编队 {preempted_by} 顶出，批准编号 {basis} 已保留")

    def reinstate_formation(self, formation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准复队")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "reason 必填")
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            if formation["status"] != "displaced": raise ApiError(409, "invalid_transition", "只有被顶出编队可以复队")
            basis_id = formation["basis_approval_id"]
            if not basis_id: raise ApiError(409, "basis_missing", "原批准依据缺失，需要重新整队审批")
            basis = conn.execute("SELECT * FROM formation_approvals WHERE id=?", (basis_id,)).fetchone()
            if not basis: raise ApiError(409, "basis_missing", "原批准依据不存在")
            members = self._member_rows(conn, formation_id)
            snapshot = json.loads(basis["member_snapshot_json"])
            revisions = {s["plan_id"]: s["revision"] for s in snapshot}
            stale = [m["id"] for m in members if revisions.get(m["id"]) != m["revision"]]
            if stale or {m["id"] for m in members} != set(revisions):
                raise ApiError(409, "basis_stale", "成员在顶出期间发生改动或拆并，原批准依据失效，需要重新整队审批", {"stale_members": stale})
            report = self._formation_conflict_report(conn, formation, members)
            if report["member_issues"]: raise ApiError(409, "formation_member_conflict", "成员计划存在冲突，不能凭原依据复队", report)
            if any(c["remaining"] < c["needed"] for c in report["capacity"]): raise ApiError(409, "capacity_full", "当前空域名额仍不足，无法复队", report)
            now = iso()
            cur = conn.execute("""INSERT INTO formation_approvals(formation_id,formation_revision,reviewer,decision,reason,override_kind,basis_approval_id,member_snapshot_json,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?)""", (formation_id, formation["revision"], actor, "approved", f"复队：{reason}", "reinstatement", basis_id, basis["member_snapshot_json"], now))
            new_approval_id = cur.lastrowid
            self._hold_occupancy(conn, new_approval_id, formation_id, members)
            for m in members:
                conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (now, m["plan_id"]))
                conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,formation_approval_id,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?)""", (m["plan_id"], m["revision"], actor, "approved", f"凭原批准 {basis_id} 复队：{reason}", None, "reinstatement", new_approval_id, now))
            conn.execute("UPDATE formations SET status='approved',displaced_by_formation_id=NULL,displaced_at=NULL,basis_approval_id=?,last_approved_at=?,updated_at=? WHERE id=?",
                         (new_approval_id, now, now, formation_id))
            for m in members: Repository.notify(conn, m["plan_id"], "formation_reinstated", f"编队 {formation['name']} 已凭原批准依据复队")
            Repository.audit(conn, None, actor, role, "formation_reinstated", {"formation_id": formation_id, "basis_approval_id": basis_id, "new_approval_id": new_approval_id})
            return {"formation": self.formation_dict(conn, self._formation_row(conn, formation_id)), "formation_approval_id": new_approval_id, "basis_approval_id": basis_id}

    def split_formation(self, formation_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以拆分编队")
        name = str(body.get("name", "")).strip()
        plan_ids, leader = body.get("plan_ids"), body.get("leader_plan_id")
        if not name or not isinstance(plan_ids, list) or not plan_ids or any(not isinstance(x, int) for x in plan_ids): raise ApiError(400, "split_details_required", "新编队 name 和 plan_ids 必填")
        plan_ids = list(dict.fromkeys(plan_ids))
        if not isinstance(leader, int) or leader not in plan_ids: raise ApiError(400, "leader_required", "新编队 leader_plan_id 必须是迁出成员之一")
        with self.repo.tx() as conn:
            source = self._formation_row(conn, formation_id)
            if source["operator_id"] != operator: raise ApiError(403, "formation_forbidden", "不能拆分其他运营方编队")
            if source["status"] in {"canceled", "expired"}: raise ApiError(409, "formation_closed", "已关闭编队不能拆分")
            rows = conn.execute(f"SELECT plan_id FROM formation_members WHERE formation_id=? AND plan_id IN ({','.join('?' for _ in plan_ids)})", (formation_id, *plan_ids)).fetchall()
            if len(rows) != len(plan_ids): raise ApiError(404, "member_not_found", "部分计划不在该编队中")
            remaining = conn.execute("SELECT COUNT(*) c FROM formation_members WHERE formation_id=?", (formation_id,)).fetchone()["c"] - len(plan_ids)
            if remaining < 1: raise ApiError(409, "formation_too_small", "原编队必须至少保留一个成员；整队迁走请使用合并")
            was_live = source["status"] in {"approved", "displaced"}
            if was_live: self._invalidate_formation_approval(conn, formation_id, actor, role, "formation_split", {"split_members": plan_ids})
            now = iso()
            cur = conn.execute("""INSERT INTO formations(name,operator_id,leader_plan_id,status,created_by,created_at,updated_at)
                                  VALUES(?,?,?,?,?,?,?)""", (name, source["operator_id"], leader, "forming", actor, now, now))
            new_id = cur.lastrowid
            for pid in plan_ids:
                conn.execute("UPDATE formation_members SET formation_id=?,role=?,snapshot_revision=NULL,joined_at=? WHERE plan_id=?",
                             (new_id, "leader" if pid == leader else "member", now, pid))
                conn.execute("UPDATE flight_plans SET status='draft',updated_at=? WHERE id=?", (now, pid))
            if was_live or source["leader_plan_id"] in plan_ids or source["status"] == "submitted":
                left = conn.execute("SELECT plan_id FROM formation_members WHERE formation_id=? ORDER BY plan_id LIMIT 1", (formation_id,)).fetchone()
                conn.execute("UPDATE formation_members SET role='member' WHERE formation_id=?", (formation_id,))
                conn.execute("UPDATE formation_members SET role='leader' WHERE formation_id=? AND plan_id=?", (formation_id, left["plan_id"]))
                conn.execute("""UPDATE formations SET leader_plan_id=?,status='forming',basis_approval_id=NULL,displaced_by_formation_id=NULL,
                                displaced_at=NULL,last_approved_at=NULL,updated_at=? WHERE id=?""", (left["plan_id"], now, formation_id))
                for m in self._member_rows(conn, formation_id):
                    conn.execute("UPDATE flight_plans SET status='draft',updated_at=? WHERE id=? AND status IN ('submitted','approved','displaced')", (now, m["plan_id"]))
            Repository.audit(conn, None, actor, role, "formation_split_done", {"source_formation_id": formation_id, "new_formation_id": new_id, "members": plan_ids, "invalidated": was_live})
            return {"source": self.formation_dict(conn, self._formation_row(conn, formation_id)), "new": self.formation_dict(conn, self._formation_row(conn, new_id)), "approval_invalidated": was_live}

    def merge_formations(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以合并编队")
        name, formation_ids, leader = str(body.get("name", "")).strip(), body.get("formation_ids"), body.get("leader_plan_id")
        if not name or not isinstance(formation_ids, list) or len(formation_ids) < 2 or any(not isinstance(x, int) for x in formation_ids): raise ApiError(400, "merge_details_required", "合并需要 name 和至少两个 formation_ids")
        formation_ids = list(dict.fromkeys(formation_ids))
        with self.repo.tx() as conn:
            groups = [self._formation_row(conn, fid) for fid in formation_ids]
            ops = {g["operator_id"] for g in groups}
            if ops != {operator}: raise ApiError(403, "formation_forbidden", "只能合并且同一运营方的编队")
            if any(g["status"] in {"submitted", "canceled", "expired"} for g in groups): raise ApiError(409, "formation_locked", "已提交未审批或已关闭的编队不能合并")
            member_rows = conn.execute(f"SELECT * FROM formation_members WHERE formation_id IN ({','.join('?' for _ in formation_ids)}) ORDER BY formation_id,plan_id", formation_ids).fetchall()
            plan_ids = [m["plan_id"] for m in member_rows]
            if not isinstance(leader, int) or leader not in plan_ids: raise ApiError(400, "leader_required", "leader_plan_id 必须是合并后成员之一")
            invalidated_any = False
            for g in groups:
                if g["status"] in {"approved", "displaced"}:
                    self._invalidate_formation_approval(conn, g["id"], actor, role, "formation_merged_away", {"merged_into_new": True}); invalidated_any = True
            now = iso()
            cur = conn.execute("""INSERT INTO formations(name,operator_id,leader_plan_id,status,created_by,created_at,updated_at)
                                  VALUES(?, ?,?, 'forming',?,?,?)""", (name, operator, leader, actor, now, now))
            new_id = cur.lastrowid
            for m in member_rows:
                conn.execute("UPDATE formation_members SET formation_id=?,role=?,snapshot_revision=NULL,joined_at=? WHERE plan_id=?",
                             (new_id, "leader" if m["plan_id"] == leader else "member", now, m["plan_id"]))
                conn.execute("UPDATE flight_plans SET status='draft',updated_at=? WHERE id=?", (now, m["plan_id"]))
            Repository.audit(conn, None, actor, role, "formation_merged", {"new_formation_id": new_id, "sources": formation_ids, "invalidated": invalidated_any})
            return {"formation": self.formation_dict(conn, self._formation_row(conn, new_id)), "source_formation_ids": formation_ids, "approvals_invalidated": invalidated_any}

    def dissolve_formation(self, formation_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "解散原因必填")
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            if role == "operator" and formation["operator_id"] != operator: raise ApiError(403, "formation_forbidden", "不能解散其他运营方编队")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "formation_forbidden", "当前角色不能解散编队")
            if formation["status"] in {"canceled", "expired"}: return {"idempotent": True, "formation_id": formation_id}
            was_live = formation["status"] in {"approved", "displaced"}
            if was_live: self._invalidate_formation_approval(conn, formation_id, actor, role, "formation_dissolved", {"reason": reason})
            now = iso()
            members = self._member_rows(conn, formation_id)
            for m in members:
                conn.execute("UPDATE flight_plans SET status='draft',updated_at=? WHERE id=? AND status IN ('submitted','approved','displaced')", (now, m["plan_id"]))
            conn.execute("DELETE FROM formation_members WHERE formation_id=?", (formation_id,))
            conn.execute("UPDATE formations SET status='canceled',basis_approval_id=NULL,displaced_by_formation_id=NULL,displaced_at=NULL,last_approved_at=NULL,updated_at=? WHERE id=?", (now, formation_id))
            Repository.audit(conn, None, actor, role, "formation_dissolved_done", {"formation_id": formation_id, "reason": reason})
            return {"formation_id": formation_id, "idempotent": False, "released_capacity": was_live}

    def create_capacity_window(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "capacity_forbidden", "只有空域审核员或指挥官可以配置空域名额")
        region = str(body.get("region", "")).strip()
        if not region: raise ApiError(400, "missing_fields", "region 必填")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        try: slots = int(body.get("slots"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_slots", "slots 必须为整数")
        if end <= start or slots < 1: raise ApiError(400, "invalid_window", "窗口时间或名额无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO capacity_windows(region,starts_at,ends_at,slots,created_by,created_at)
                                  VALUES(?,?,?,?,?,?)""", (region, iso(start), iso(end), slots, actor, iso()))
            return dict(conn.execute("SELECT * FROM capacity_windows WHERE id=?", (cur.lastrowid,)).fetchone())

    def get_formation(self, formation_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; formation = self._formation_row(conn, formation_id)
        if role == "operator" and formation["operator_id"] != operator: raise ApiError(403, "formation_forbidden", "不能查看其他运营方编队")
        result = self.formation_dict(conn, formation)
        if role in {"airspace_reviewer", "commander", "auditor"}:
            result["approvals"] = [dict(r) for r in conn.execute("SELECT * FROM formation_approvals WHERE formation_id=? ORDER BY id", (formation_id,))]
        return result

    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso()
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE status='approved' AND ends_at<=?", (now,)))
            expired_teams: set[int] = set()
            for row in rows:
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
                member = conn.execute("SELECT formation_id FROM formation_members WHERE plan_id=?", (row["id"],)).fetchone()
                if member: expired_teams.add(member["formation_id"])
            for fid in expired_teams:
                formation = conn.execute("SELECT * FROM formations WHERE id=?", (fid,)).fetchone()
                if formation and formation["status"] == "approved":
                    if formation["basis_approval_id"]: self._release_occupancy(conn, formation["basis_approval_id"])
                    conn.execute("UPDATE formations SET status='expired',basis_approval_id=NULL,last_approved_at=NULL,updated_at=? WHERE id=?", (now, fid))
                    Repository.audit(conn, None, actor, role, "formation_expired", {"formation_id": fid})
                    for m in self._member_rows(conn, fid):
                        Repository.notify(conn, m["plan_id"], "formation_expired", f"编队 {formation['name']} 已到期，名额已释放")
        return {"expired": len(rows), "formations_expired": len(expired_teams)}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        for row in rows:
            item = self.get_plan(row["id"], role, operator); plans.append(item)
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        if role == "operator": frows = conn.execute("SELECT * FROM formations WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: frows = conn.execute("SELECT * FROM formations ORDER BY id DESC")
        else: frows = conn.execute("SELECT * FROM formations WHERE status='approved' ORDER BY id DESC")
        formations = [self.formation_dict(conn, f, include_members=(role != "viewer")) for f in frows]
        windows = [dict(w) for w in conn.execute("SELECT * FROM capacity_windows ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        return {"plans": plans, "formations": formations, "restrictions": restrictions, "capacity_windows": windows, "server_time": iso()}


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(); handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: DroneAirspaceService; web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if not size: return {}
        try: value = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "drone-airspace"}
        actor, role, operator = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state(role, operator)
        if path == "/api/notifications": return 200, self.service.notifications(actor, role, operator)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_conflicts(int(parts[2]), role, operator)
        if len(parts) == 3 and parts[:2] == ["api", "formations"] and parts[2].isdigit(): return 200, self.service.get_formation(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "formations"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_formation(int(parts[2]), role, operator)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role, operator = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        if path == "/api/restrictions": return 201, self.service.create_restriction(actor, role, body)
        if path == "/api/capacity-windows": return 201, self.service.create_capacity_window(actor, role, body)
        if path == "/api/plans": return 201, self.service.create_plan(actor, role, operator, body)
        if path == "/api/formations": return 201, self.service.create_formation(actor, role, operator, body)
        if path == "/api/formations/merge": return 200, self.service.merge_formations(actor, role, operator, body)
        if path == "/api/expire": return 200, self.service.expire_plans(actor, role)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            pid, action = int(parts[2]), parts[3]
            routes = {
                "submit": lambda: self.service.submit(pid, actor, role, operator, body),
                "approve": lambda: self.service.approve(pid, actor, role, body),
                "reject": lambda: self.service.reject(pid, actor, role, body),
                "change": lambda: self.service.change(pid, actor, role, operator, body),
                "cancel": lambda: self.service.cancel(pid, actor, role, operator, body),
            }
            if action in routes: return 200, routes[action]()
        if len(parts) >= 3 and parts[:2] == ["api", "formations"] and parts[2].isdigit():
            fid, action = int(parts[2]), parts[3] if len(parts) == 4 else None
            froutes = {
                "submit": lambda: self.service.submit_formation(fid, actor, role, operator),
                "approve": lambda: self.service.approve_formation(fid, actor, role, body),
                "reinstate": lambda: self.service.reinstate_formation(fid, actor, role, body),
                "split": lambda: self.service.split_formation(fid, actor, role, operator, body),
                "members/add": lambda: self.service.add_member(fid, actor, role, operator, body),
                "members/remove": lambda: self.service.remove_member(fid, actor, role, operator, body),
                "dissolve": lambda: self.service.dissolve_formation(fid, actor, role, operator, body),
            }
            key = action if len(parts) == 4 and parts[3] in ("submit", "approve", "reinstate", "split", "dissolve") else (f"{parts[3]}/{parts[4]}" if len(parts) == 5 and parts[3] == "members" else None)
            if key in froutes: return 200, froutes[key]()
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path); send_json(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            send_json(self, exc.status, payload)
        except Exception as exc: print(f"unhandled error: {exc!r}"); send_json(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = DroneAirspaceService(db_path); handler = type("DroneHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"}); return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("DRONE_DB", "drone_airspace.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"drone-airspace listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
