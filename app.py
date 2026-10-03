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
# 同一片重叠空域内允许同时运行的编队数量；编队（而非单机）是容量计量单位
FORMATION_CAPACITY = 2
FORMATION_ACTIVE_STATUSES = {"forming", "submitted", "approved", "invalidated", "bumped"}
FORMATION_BBOX_BUFFER = 0.002


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
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row; self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'standard',
            leader_plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            status TEXT NOT NULL DEFAULT 'forming', revision INTEGER NOT NULL DEFAULT 1,
            offline_id TEXT, approved_by TEXT, approved_at TEXT,
            bumped_reason TEXT, bumped_by_formation_id INTEGER, bumped_at TEXT,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formation_members(
            formation_id INTEGER NOT NULL REFERENCES formations(id),
            plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            role TEXT NOT NULL DEFAULT 'member', joined_at TEXT NOT NULL,
            PRIMARY KEY (formation_id, plan_id)
        );
        """)
        # 旧库迁移：批准记录关联编队
        try: self.conn.execute("ALTER TABLE approvals ADD COLUMN formation_id INTEGER REFERENCES formations(id)")
        except sqlite3.OperationalError: pass

    @contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn; self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)", (plan_id, kind, message, iso()))


class DroneAirspaceService:
    def __init__(self, path: str | Path, formation_capacity: int = FORMATION_CAPACITY):
        self.repo = Repository(path)
        self.formation_capacity = formation_capacity

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

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row, include_adjacent: bool = True) -> dict[str, Any]:
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
        if include_adjacent:
            formation = self._active_formation_for_plan(conn, plan["id"])
            mate_ids = {m["plan_id"] for m in self._formation_members(conn, formation["id"])} if formation else set()
            for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
                if other["id"] in mate_ids: continue
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
            existing_formation = self._active_formation_for_plan(conn, plan_id)
            if existing_formation:
                raise ApiError(409, "plan_in_formation", f"计划属于编队 {existing_formation['name']}，请通过编队提交")
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
            existing_formation = self._active_formation_for_plan(conn, plan_id)
            if existing_formation:
                raise ApiError(409, "plan_in_formation", f"计划属于编队 {existing_formation['name']}，请通过编队整队批准")
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
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), revision, iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"]})
            existing_formation = self._active_formation_for_plan(conn, plan_id)
            if existing_formation and existing_formation["status"] in {"approved", "submitted"}:
                self._invalidate_formation(conn, plan_id, f"成员 {plan['callsign']} 变更时间或航线", plan_id, actor, role)
            if plan["status"] == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
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
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason})
            existing_formation = self._active_formation_for_plan(conn, plan_id)
            if existing_formation and existing_formation["status"] in {"approved", "submitted"}:
                self._invalidate_formation(conn, plan_id, f"成员 {plan['callsign']} 被取消", plan_id, actor, role)
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

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
            for row in rows:
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
        return {"expired": len(rows)}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        for row in rows:
            item = self.get_plan(row["id"], role, operator); plans.append(item)
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        return {"plans": plans, "restrictions": restrictions, "server_time": iso()}

    # ---- 编队：绑定成员、整队放行、容量占用与失效 ----

    def _formation_row(self, conn: sqlite3.Connection, formation_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM formations WHERE id=?", (formation_id,)).fetchone()
        if not row: raise ApiError(404, "formation_not_found", "编队不存在")
        return row

    def _formation_members(self, conn: sqlite3.Connection, formation_id: int) -> list[sqlite3.Row]:
        return list(conn.execute("""SELECT m.formation_id,m.plan_id,m.role,m.joined_at,
                                          p.callsign,p.operator_id,p.status AS plan_status,
                                          p.starts_at,p.ends_at,p.route_json,p.max_altitude,p.revision AS plan_revision
                                   FROM formation_members m JOIN flight_plans p ON p.id=m.plan_id
                                   WHERE m.formation_id=? ORDER BY CASE m.role WHEN 'leader' THEN 0 ELSE 1 END, m.plan_id""",
                                (formation_id,)))

    def _active_formation_for_plan(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
        return conn.execute("""SELECT f.* FROM formations f JOIN formation_members m ON m.formation_id=f.id
                                WHERE m.plan_id=? AND f.status IN (%s)""" % ",".join("?" * len(FORMATION_ACTIVE_STATUSES)),
                            (plan_id, *FORMATION_ACTIVE_STATUSES)).fetchone()

    def _formation_bbox(self, conn: sqlite3.Connection, formation_id: int) -> tuple[tuple[float, float, float, float], datetime, datetime]:
        members = self._formation_members(conn, formation_id)
        if not members: raise ApiError(409, "formation_empty", "编队没有成员")
        bboxes = [route_bbox(json.loads(m["route_json"])) for m in members]
        start = min(parse_time(m["starts_at"]) for m in members)
        end = max(parse_time(m["ends_at"]) for m in members)
        return (min(b[0] for b in bboxes), min(b[1] for b in bboxes), max(b[2] for b in bboxes), max(b[3] for b in bboxes)), start, end

    def _overlapping_approved_formations(self, conn: sqlite3.Connection, formation_id: int,
                                        bbox: tuple[float, float, float, float], start: datetime, end: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for f in conn.execute("SELECT * FROM formations WHERE status='approved' AND id!=? ORDER BY approved_at DESC, id DESC", (formation_id,)):
            obbox, ostart, oend = self._formation_bbox(conn, f["id"])
            if not boxes_overlap(bbox, obbox, FORMATION_BBOX_BUFFER) or not times_overlap(start, end, ostart, oend):
                continue
            conflicting_members = []
            for m in self._formation_members(conn, f["id"]):
                mbox = route_bbox(json.loads(m["route_json"]))
                if boxes_overlap(bbox, mbox, FORMATION_BBOX_BUFFER) and times_overlap(start, end, parse_time(m["starts_at"]), parse_time(m["ends_at"])):
                    conflicting_members.append({"plan_id": m["plan_id"], "callsign": m["callsign"], "role": m["role"]})
            out.append({"formation_id": f["id"], "name": f["name"], "kind": f["kind"],
                        "leader_plan_id": f["leader_plan_id"], "approved_at": f["approved_at"],
                        "conflicting_members": conflicting_members})
        return out

    def _formation_conflict_check(self, conn: sqlite3.Connection, formation_id: int) -> dict[str, Any]:
        members = self._formation_members(conn, formation_id)
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        for m in members:
            plan = self._plan_row(conn, m["plan_id"])
            report = self._conflict_report(conn, plan, include_adjacent=False)
            hard.extend(report["hard_violations"]); blocking.extend(report["blocking_conflicts"])
        bbox, start, end = self._formation_bbox(conn, formation_id)
        overlaps = self._overlapping_approved_formations(conn, formation_id, bbox, start, end)
        used = len(overlaps); remaining = self.formation_capacity - used
        return {"formation_id": formation_id, "hard_violations": hard, "blocking_conflicts": blocking,
                "capacity": self.formation_capacity, "used": used, "remaining_capacity": remaining,
                "conflicting_formations": overlaps, "approvable": not hard and not blocking and remaining > 0}

    def _invalidate_formation(self, conn: sqlite3.Connection, formation_id: int, reason: str,
                              except_plan_id: int | None, actor: str, role: str) -> None:
        formation = self._formation_row(conn, formation_id)
        if formation["status"] not in {"approved", "submitted"}:
            return
        conn.execute("UPDATE formations SET status='invalidated', updated_at=? WHERE id=?", (iso(), formation_id))
        for m in self._formation_members(conn, formation_id):
            if except_plan_id is not None and m["plan_id"] == except_plan_id:
                continue
            plan = self._plan_row(conn, m["plan_id"])
            if plan["status"] == "approved":
                conn.execute("UPDATE flight_plans SET status='submitted', updated_at=? WHERE id=?", (iso(), m["plan_id"]))
            Repository.notify(conn, m["plan_id"], "formation_invalidated", f"编队 {formation['name']} {reason}，整队批准作废，需重新提交审核")
        Repository.audit(conn, None, actor, role, "formation_invalidated", {"formation_id": formation_id, "reason": reason})

    def _bump_formation(self, conn: sqlite3.Connection, formation_id: int, reason: str,
                        by_formation_id: int, actor: str, role: str) -> None:
        formation = self._formation_row(conn, formation_id)
        if formation["status"] != "approved":
            return
        conn.execute("""UPDATE formations SET status='bumped', bumped_reason=?, bumped_by_formation_id=?, bumped_at=?, updated_at=?
                        WHERE id=?""", (reason, by_formation_id, iso(), iso(), formation_id))
        for m in self._formation_members(conn, formation_id):
            Repository.notify(conn, m["plan_id"], "formation_bumped", f"编队 {formation['name']} 因 {reason} 被顶出，原批准依据保留")
        Repository.audit(conn, None, actor, role, "formation_bumped",
                         {"formation_id": formation_id, "by_formation_id": by_formation_id, "reason": reason})

    def create_formation(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以组建编队")
        name = str(body.get("name", "")).strip(); kind = str(body.get("kind", "standard")).strip()
        plan_ids = body.get("member_plan_ids")
        if not name: raise ApiError(400, "formation_name_required", "编队名称必填")
        if kind not in {"standard", "rescue"}: raise ApiError(400, "invalid_formation_kind", "编队类型必须是 standard 或 rescue")
        if not isinstance(plan_ids, list) or not plan_ids: raise ApiError(400, "formation_members_required", "编队至少绑定一个成员计划")
        try: ids = [int(x) for x in plan_ids]
        except (TypeError, ValueError): raise ApiError(400, "invalid_formation_members", "成员计划 ID 必须是数字")
        if len(set(ids)) != len(ids): raise ApiError(400, "formation_members_duplicate", "编队成员不能重复")
        leader = body.get("leader_plan_id")
        if leader is not None:
            try: leader = int(leader)
            except (TypeError, ValueError): raise ApiError(400, "invalid_leader", "队长计划 ID 必须是数字")
        with self.repo.tx() as conn:
            for pid in ids:
                plan = self._plan_row(conn, pid)
                if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能绑定其他运营方的计划")
                if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "plan_not_editable", f"计划 {plan['callsign']} 已在流程中，不能编入新编队")
                existing = self._active_formation_for_plan(conn, pid)
                if existing: raise ApiError(409, "plan_in_formation", f"计划 {plan['callsign']} 已在编队 {existing['name']} 中")
            if leader is None: leader = ids[0]
            if leader not in ids: raise ApiError(400, "invalid_leader", "队长必须是编队成员")
            cur = conn.execute("""INSERT INTO formations(name,kind,leader_plan_id,status,created_by,created_at,updated_at)
                                  VALUES(?,?,?,?,?,?,?)""", (name, kind, leader, "forming", actor, iso(), iso()))
            fid = cur.lastrowid
            for pid in ids:
                conn.execute("INSERT INTO formation_members(formation_id,plan_id,role,joined_at) VALUES(?,?,?,?)",
                             (fid, pid, "leader" if pid == leader else "member", iso()))
            Repository.audit(conn, None, actor, role, "formation_created", {"formation_id": fid, "name": name, "members": ids})
            return self.get_formation(fid, role, operator)

    def list_formations(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        rows = conn.execute("SELECT * FROM formations ORDER BY id DESC").fetchall()
        formations = []
        for row in rows:
            members = self._formation_members(conn, row["id"])
            if role == "operator" and any(m["operator_id"] != operator for m in members):
                continue
            formations.append(self.get_formation(row["id"], role, operator))
        return {"formations": formations}

    def get_formation(self, formation_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn
        formation = self._formation_row(conn, formation_id)
        members = [dict(m) for m in self._formation_members(conn, formation_id)]
        if role == "operator" and any(m["operator_id"] != operator for m in members):
            raise ApiError(403, "formation_forbidden", "不能查看其他运营方的编队")
        result = dict(formation); result["members"] = members
        return result

    def submit_formation(self, formation_id: int, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以提交编队")
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            members = self._formation_members(conn, formation_id)
            if any(m["operator_id"] != operator for m in members): raise ApiError(403, "formation_forbidden", "不能提交其他运营方的编队")
            if formation["status"] not in {"forming", "invalidated"}: raise ApiError(409, "invalid_transition", "当前编队状态不能提交")
            for m in members:
                plan = self._plan_row(conn, m["plan_id"])
                if plan["status"] in {"draft", "rejected"}:
                    conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), m["plan_id"]))
            conn.execute("UPDATE formations SET status='submitted',updated_at=? WHERE id=?", (iso(), formation_id))
            Repository.audit(conn, None, actor, role, "formation_submitted", {"formation_id": formation_id, "revision": formation["revision"]})
            for m in members: Repository.notify(conn, m["plan_id"], "formation_submitted", f"编队 {formation['name']} 已整队提交，等待批准")
            return self.get_formation(formation_id, role, operator)

    def approve_formation(self, formation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准编队")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason:
            raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            if formation["status"] == "approved" and formation["offline_id"] == offline_id:
                return {"formation": self.get_formation(formation_id, role, ""), "idempotent": True, "approval_id": None}
            if formation["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交编队可以批准")
            if formation["revision"] != expected: raise ApiError(409, "revision_conflict", "编队版本已变化，审核决定不能套用")
            check = self._formation_conflict_check(conn, formation_id)
            if check["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "编队存在不可覆盖的安全约束", check)
            if check["blocking_conflicts"] and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "编队存在空域或相邻交通冲突", check)
            bbox, start, end = self._formation_bbox(conn, formation_id)
            overlaps = self._overlapping_approved_formations(conn, formation_id, bbox, start, end)
            remaining = self.formation_capacity - len(overlaps)
            bumped: list[dict[str, Any]] = []
            if remaining <= 0:
                if role == "commander" and override:
                    for target in overlaps[:1]:
                        self._bump_formation(conn, target["formation_id"],
                                             f"救援队 {formation['name']} 紧急插入顶出名额", formation_id, actor, role)
                        bumped.append(target)
                else:
                    raise ApiError(409, "formation_capacity_conflict", "空域编队容量已满，无法整队放行",
                                   {"capacity": self.formation_capacity, "used": len(overlaps),
                                    "remaining_capacity": remaining, "conflicting_formations": overlaps})
            override_kind = "emergency_authority" if (role == "commander" and override and check["blocking_conflicts"]) else None
            for m in self._formation_members(conn, formation_id):
                plan = self._plan_row(conn, m["plan_id"])
                conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,formation_id,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?)""",
                             (m["plan_id"], plan["revision"], actor, "approved", reason, f"{offline_id}:{m['plan_id']}", override_kind, formation_id, iso()))
                conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), m["plan_id"]))
            conn.execute("UPDATE formations SET status='approved',offline_id=?,approved_by=?,approved_at=?,updated_at=? WHERE id=?",
                         (offline_id, actor, iso(), iso(), formation_id))
            Repository.audit(conn, None, actor, role, "formation_approved",
                             {"formation_id": formation_id, "revision": expected, "offline_id": offline_id,
                              "members": [m["plan_id"] for m in self._formation_members(conn, formation_id)],
                              "bumped_formations": [b["formation_id"] for b in bumped]})
            for m in self._formation_members(conn, formation_id):
                Repository.notify(conn, m["plan_id"], "approved", f"编队 {formation['name']} 整队批准")
            return {"formation": self.get_formation(formation_id, role, ""), "idempotent": False,
                    "bumped_formations": bumped, "override_kind": override_kind}

    def reinstate_formation(self, formation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以恢复编队")
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            if formation["status"] != "bumped": raise ApiError(409, "invalid_transition", "只有被顶出的编队可以恢复")
            bbox, start, end = self._formation_bbox(conn, formation_id)
            overlaps = self._overlapping_approved_formations(conn, formation_id, bbox, start, end)
            remaining = self.formation_capacity - len(overlaps)
            if remaining <= 0:
                raise ApiError(409, "formation_capacity_conflict", "空域编队容量已满，无法恢复",
                               {"capacity": self.formation_capacity, "used": len(overlaps),
                                "remaining_capacity": remaining, "conflicting_formations": overlaps})
            conn.execute("UPDATE formations SET status='approved',bumped_reason=NULL,bumped_by_formation_id=NULL,bumped_at=NULL,updated_at=? WHERE id=?",
                         (iso(), formation_id))
            Repository.audit(conn, None, actor, role, "formation_reinstated", {"formation_id": formation_id})
            for m in self._formation_members(conn, formation_id):
                Repository.notify(conn, m["plan_id"], "formation_reinstated", f"编队 {formation['name']} 已恢复，原批准依据继续有效")
            return {"formation": self.get_formation(formation_id, role, "")}

    def split_formation(self, formation_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以拆分编队")
        name = str(body.get("name", "")).strip(); split_ids = body.get("member_plan_ids")
        if not name or not isinstance(split_ids, list) or not split_ids: raise ApiError(400, "split_details_required", "新编队名称和拆出成员必填")
        try: split_ids = [int(x) for x in split_ids]
        except (TypeError, ValueError): raise ApiError(400, "invalid_formation_members", "成员计划 ID 必须是数字")
        leader = body.get("leader_plan_id")
        if leader is not None:
            try: leader = int(leader)
            except (TypeError, ValueError): raise ApiError(400, "invalid_leader", "队长计划 ID 必须是数字")
        with self.repo.tx() as conn:
            formation = self._formation_row(conn, formation_id)
            members = self._formation_members(conn, formation_id)
            if any(m["operator_id"] != operator for m in members): raise ApiError(403, "formation_forbidden", "不能拆分其他运营方的编队")
            current_ids = [m["plan_id"] for m in members]
            if any(pid not in current_ids for pid in split_ids): raise ApiError(400, "invalid_formation_members", "拆出成员必须是当前编队成员")
            remaining = [pid for pid in current_ids if pid not in split_ids]
            if not remaining: raise ApiError(400, "split_empty", "不能把全部成员拆出，原编队至少保留一架")
            if formation["status"] == "approved":
                self._invalidate_formation(conn, formation_id, "因编队拆分", None, actor, role)
            for pid in split_ids:
                conn.execute("DELETE FROM formation_members WHERE formation_id=? AND plan_id=?", (formation_id, pid))
            if leader is None or leader not in split_ids: leader = split_ids[0]
            cur = conn.execute("INSERT INTO formations(name,kind,leader_plan_id,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                               (name, formation["kind"], leader, "forming", actor, iso(), iso()))
            new_id = cur.lastrowid
            for pid in split_ids:
                conn.execute("INSERT INTO formation_members(formation_id,plan_id,role,joined_at) VALUES(?,?,?,?)",
                             (new_id, pid, "leader" if pid == leader else "member", iso()))
            old_check = self._formation_conflict_check(conn, formation_id)
            new_check = self._formation_conflict_check(conn, new_id)
            Repository.audit(conn, None, actor, role, "formation_split",
                             {"formation_id": formation_id, "new_formation_id": new_id, "split_members": split_ids})
            return {"old_formation": self.get_formation(formation_id, role, operator),
                    "new_formation": self.get_formation(new_id, role, operator),
                    "old_conflicts": old_check, "new_conflicts": new_check}

    def merge_formations(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "formation_forbidden", "只有运营方可以合并编队")
        name = str(body.get("name", "")).strip(); formation_ids = body.get("formation_ids")
        if not name or not isinstance(formation_ids, list) or len(formation_ids) < 2:
            raise ApiError(400, "merge_details_required", "新编队名称和至少两个源编队必填")
        try: formation_ids = [int(x) for x in formation_ids]
        except (TypeError, ValueError): raise ApiError(400, "invalid_formations", "编队 ID 必须是数字")
        leader = body.get("leader_plan_id")
        if leader is not None:
            try: leader = int(leader)
            except (TypeError, ValueError): raise ApiError(400, "invalid_leader", "队长计划 ID 必须是数字")
        with self.repo.tx() as conn:
            all_members: list[int] = []; kind = "standard"
            for fid in formation_ids:
                formation = self._formation_row(conn, fid)
                members = self._formation_members(conn, fid)
                if any(m["operator_id"] != operator for m in members): raise ApiError(403, "formation_forbidden", "不能合并其他运营方的编队")
                if formation["status"] == "approved":
                    self._invalidate_formation(conn, fid, "因编队合并", None, actor, role)
                for m in members:
                    if m["plan_id"] not in all_members: all_members.append(m["plan_id"])
                if formation["kind"] == "rescue": kind = "rescue"
            if len(all_members) < 2: raise ApiError(400, "merge_empty", "合并后至少需要两名成员")
            for fid in formation_ids:
                conn.execute("UPDATE formations SET status='dissolved',updated_at=? WHERE id=?", (iso(), fid))
                conn.execute("DELETE FROM formation_members WHERE formation_id=?", (fid,))
            if leader is None or leader not in all_members: leader = all_members[0]
            cur = conn.execute("INSERT INTO formations(name,kind,leader_plan_id,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                               (name, kind, leader, "forming", actor, iso(), iso()))
            new_id = cur.lastrowid
            for pid in all_members:
                conn.execute("INSERT INTO formation_members(formation_id,plan_id,role,joined_at) VALUES(?,?,?,?)",
                             (new_id, pid, "leader" if pid == leader else "member", iso()))
            check = self._formation_conflict_check(conn, new_id)
            Repository.audit(conn, None, actor, role, "formation_merged",
                             {"formation_ids": formation_ids, "new_formation_id": new_id, "members": all_members})
            return {"formation": self.get_formation(new_id, role, operator), "conflicts": check}


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
        if path == "/api/formations": return 200, self.service.list_formations(role, operator)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "formations"] and parts[2].isdigit():
            return 200, self.service.get_formation(int(parts[2]), role, operator)
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_conflicts(int(parts[2]), role, operator)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role, operator = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        if path == "/api/restrictions": return 201, self.service.create_restriction(actor, role, body)
        if path == "/api/plans": return 201, self.service.create_plan(actor, role, operator, body)
        if path == "/api/expire": return 200, self.service.expire_plans(actor, role)
        if path == "/api/formations": return 201, self.service.create_formation(actor, role, operator, body)
        if path == "/api/formations/merge": return 200, self.service.merge_formations(actor, role, operator, body)
        if len(parts) == 4 and parts[:2] == ["api", "formations"] and parts[2].isdigit():
            fid, action = int(parts[2]), parts[3]
            routes = {
                "submit": lambda: self.service.submit_formation(fid, actor, role, operator),
                "approve": lambda: self.service.approve_formation(fid, actor, role, body),
                "split": lambda: self.service.split_formation(fid, actor, role, operator, body),
                "reinstate": lambda: self.service.reinstate_formation(fid, actor, role, body),
            }
            if action in routes: return 200, routes[action]()
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
