import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class FormationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db")
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, callsign, operator="OP1", route=None, region="BJ", start=None):
        start = start or self.start
        return self.svc.create_plan("op-user", "operator", operator, {
            "callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route or [[116.10, 39.80], [116.30, 39.90]],
            "starts_at": iso(start), "ends_at": iso(start + timedelta(hours=1)),
            "max_altitude": 100, "population_risk": 1,
            "emergency_plan": "返回起降点", "region": region,
        })

    def window(self, slots=6, region="BJ", start=None, end=None):
        start = start or (self.start - timedelta(minutes=30))
        end = end or (self.start + timedelta(hours=2))
        return self.svc.create_capacity_window("reviewer", "airspace_reviewer", {
            "region": region, "slots": slots, "starts_at": iso(start), "ends_at": iso(end),
        })

    def build_team(self, callsigns=("A1", "A2", "A3"), slots=6, name="巡检甲队"):
        self.window(slots=slots)
        plans = [self.plan(c) for c in callsigns]
        formation = self.svc.create_formation("op-user", "operator", "OP1", {
            "name": name, "plan_ids": [p["id"] for p in plans], "leader_plan_id": plans[0]["id"],
        })
        return plans, formation

    def approve_team(self, fid, revision=1, offline="fa-1", reviewer="reviewer", role="airspace_reviewer", **extra):
        body = {"expected_revision": revision, "offline_id": offline, "reason": "整队航线与预案合格", **extra}
        return self.svc.approve_formation(fid, reviewer, role, body)

    def submit_team(self, plans, formation):
        return self.svc.submit_formation(formation["id"], "op-user", "operator", "OP1")["formation"]

    def test_bind_submit_and_whole_team_capacity_hold(self):
        plans, formation = self.build_team()
        # 成员计划被唯一绑定，不能再进别的编队
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_formation("op-user", "operator", "OP1", {
                "name": "重复编队", "plan_ids": [plans[1]["id"]], "leader_plan_id": plans[1]["id"]})
        self.assertEqual(ctx.exception.code, "member_already_bound")

        submitted = self.submit_team(plans, formation)
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(submitted["revision"], 2)
        for member in submitted["members"]:
            self.assertEqual(member["status"], "submitted")

        check = self.svc.check_formation(formation["id"], "airspace_reviewer", "")
        self.assertTrue(check["approvable"])
        self.assertEqual(check["capacity"][0]["needed"], 3)
        self.assertEqual(check["capacity"][0]["remaining"], 6)

        result = self.approve_team(formation["id"], revision=2)
        self.assertEqual(result["formation"]["status"], "approved")
        held = self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"]
        self.assertEqual(held, 3)
        # 批准记录同时落在编队和每个成员计划上
        per_plan = self.svc.repo.conn.execute("SELECT COUNT(*) c FROM approvals WHERE formation_approval_id=?", (result["formation_approval_id"],)).fetchone()["c"]
        self.assertEqual(per_plan, 3)
        # 没有逐架审批入口占用额外名额
        for p in plans:
            self.assertEqual(self.svc.get_plan(p["id"], "operator", "OP1")["status"], "approved")

    def test_capacity_window_required_before_approval(self):
        plans = [self.plan("B1"), self.plan("B2")]
        formation = self.svc.create_formation("op-user", "operator", "OP1", {
            "name": "无窗口编队", "plan_ids": [p["id"] for p in plans], "leader_plan_id": plans[0]["id"]})
        self.submit_team(plans, formation)
        with self.assertRaises(ApiError) as ctx:
            self.approve_team(formation["id"], revision=2, offline="fa-wn")
        self.assertEqual(ctx.exception.code, "capacity_window_missing")

    def test_member_change_invalidates_whole_team_and_releases_capacity(self):
        plans, formation = self.build_team()
        self.submit_team(plans, formation)
        result = self.approve_team(formation["id"], revision=2)
        approval_id = result["formation_approval_id"]
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"], 3)

        # 队长改动其中一架的航线 -> 全队批准作废，旧占用全部释放
        changed = self.svc.change(plans[1]["id"], "op-user", "operator", "OP1", {
            "expected_revision": 1, "route": [[116.40, 39.82], [116.60, 39.92]]})
        self.assertEqual(changed["status"], "draft")
        team = self.svc.get_formation(formation["id"], "operator", "OP1")
        self.assertEqual(team["status"], "invalidated")
        self.assertIsNone(team["basis_approval_id"])
        for member in team["members"]:
            self.assertEqual(member["status"], "draft")
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"], 0)
        released = self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='released'").fetchone()["c"]
        self.assertEqual(released, 3)
        notifications = self.svc.notifications("op-user", "operator", "OP1")["notifications"]
        self.assertIn("formation_approval_invalidated", {n["kind"] for n in notifications})

        # 原批准依据作为历史保留，但已标记释放
        old = self.svc.repo.conn.execute("SELECT * FROM formation_approvals WHERE id=?", (approval_id,)).fetchone()
        self.assertEqual(old["decision"], "approved")
        # 重新整队提交后再次占满
        self.submit_team(plans, self.svc.get_formation(formation["id"], "operator", "OP1"))
        again = self.approve_team(formation["id"], revision=3, offline="fa-again")
        self.assertEqual(again["formation"]["status"], "approved")
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"], 3)

    def test_stale_snapshot_after_submit_blocks_approval(self):
        plans, formation = self.build_team()
        self.submit_team(plans, formation)
        # 提交后、批准前有成员改动：批准必须被拒并指出冲突成员
        self.svc.change(plans[2]["id"], "op-user", "operator", "OP1", {
            "expected_revision": 1, "route": [[116.41, 39.81], [116.61, 39.91]]})
        with self.assertRaises(ApiError) as ctx:
            self.approve_team(formation["id"], revision=2, offline="fa-stale")
        self.assertEqual(ctx.exception.code, "formation_members_changed")
        self.assertEqual(ctx.exception.details["stale_members"], [plans[2]["id"]])

    def test_concurrent_approval_only_one_team_holds_full_capacity(self):
        # 5 个名额，两个 3 机编队同时审批：只有一个占满，另一个拿到冲突编队和剩余容量
        plans_a, fa = self.build_team(("A1", "A2", "A3"), slots=5, name="甲队")
        plans_b = [self.plan(c, route=[[118.10 + i * 0.01, 31.10], [118.30, 31.30]]) for i, c in enumerate(("B1", "B2", "B3"))]
        fb = self.svc.create_formation("op-user", "operator", "OP1", {
            "name": "乙队", "plan_ids": [p["id"] for p in plans_b], "leader_plan_id": plans_b[0]["id"]})
        self.submit_team(plans_a, fa)
        self.submit_team(plans_b, fb)

        outcomes: dict[str, object] = {}

        def approve(key, fid, offline):
            try:
                outcomes[key] = ("ok", self.approve_team(fid, revision=2, offline=offline))
            except ApiError as exc:
                outcomes[key] = ("err", exc)

        t1 = threading.Thread(target=approve, args=("A", fa["id"], "fa-conc-1"))
        t2 = threading.Thread(target=approve, args=("B", fb["id"], "fa-conc-2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        keys = list(outcomes)
        results = [outcomes[k] for k in keys]
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1)
        self.assertEqual(sum(1 for kind, _ in results if kind == "err"), 1)
        held = self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"]
        self.assertEqual(held, 3)
        err = next(v for kind, v in results if kind == "err")
        self.assertEqual(err.code, "capacity_full")
        cap = err.details["capacity"][0]
        self.assertEqual(cap["slots"], 5)
        self.assertEqual(cap["held_by_formations"], 3)
        self.assertEqual(cap["needed"], 3)
        self.assertEqual(cap["remaining"], 2)
        winner = next(v["formation"] for kind, v in results if kind == "ok")
        conflict_ids = {c["formation_id"] for c in cap["conflicting"] if c["kind"] == "formation"}
        self.assertEqual(conflict_ids, {winner["id"]})

    def test_split_rechecks_conflict_and_invalidates(self):
        routes = [[[116.10, 39.80], [116.20, 39.90]], [[116.11, 39.81], [116.21, 39.91]],
                  [[118.10, 31.10], [118.20, 31.20]], [[118.11, 31.11], [118.21, 31.21]]]
        plans = [self.plan(c, route=r) for c, r in zip(("S1", "S2", "S3", "S4"), routes)]
        self.window(slots=4)
        formation = self.svc.create_formation("op-user", "operator", "OP1", {
            "name": "待拆队", "plan_ids": [p["id"] for p in plans], "leader_plan_id": plans[0]["id"]})
        self.submit_team(plans, formation)
        self.approve_team(formation["id"], revision=2, offline="fa-split-1")
        result = self.svc.split_formation(formation["id"], "op-user", "operator", "OP1", {
            "name": "拆分乙队", "plan_ids": [plans[2]["id"], plans[3]["id"]], "leader_plan_id": plans[2]["id"]})
        self.assertTrue(result["approval_invalidated"])
        self.assertEqual(result["source"]["status"], "forming")
        self.assertEqual(result["new"]["status"], "forming")
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"], 0)
        # 拆出的两队重新核对冲突：各 2 机都能在 4 个名额内重新审批
        src = self.svc.submit_formation(result["source"]["id"], "op-user", "operator", "OP1")["formation"]
        new = self.svc.submit_formation(result["new"]["id"], "op-user", "operator", "OP1")["formation"]
        self.assertTrue(self.svc.check_formation(src["id"], "airspace_reviewer", "")["approvable"])
        self.approve_team(src["id"], revision=src["revision"], offline="fa-split-a")
        # 甲队占用 2 个后，乙队仍有 2 个剩余
        check_b = self.svc.check_formation(new["id"], "airspace_reviewer", "")
        self.assertEqual(check_b["capacity"][0]["remaining"], 2)
        self.assertTrue(check_b["approvable"])
        self.approve_team(new["id"], revision=new["revision"], offline="fa-split-b")
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"], 4)

    def test_merge_rechecks_and_invalidates(self):
        plans_a, fa = self.build_team(("M1", "M2"), slots=4, name="甲队")
        self.submit_team(plans_a, fa)
        self.approve_team(fa["id"], revision=2, offline="fa-m-1")
        plans_b = [self.plan(c) for c in ("M3", "M4")]
        fb = self.svc.create_formation("op-user", "operator", "OP1", {
            "name": "乙队", "plan_ids": [p["id"] for p in plans_b], "leader_plan_id": plans_b[0]["id"]})
        merged = self.svc.merge_formations("op-user", "operator", "OP1", {
            "name": "合并大队", "formation_ids": [fa["id"], fb["id"]], "leader_plan_id": plans_a[0]["id"]})
        self.assertTrue(merged["approvals_invalidated"])
        self.assertEqual(merged["formation"]["status"], "forming")
        self.assertEqual(len(merged["formation"]["members"]), 4)
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"], 0)
        submitted = self.svc.submit_formation(merged["formation"]["id"], "op-user", "operator", "OP1")["formation"]
        self.assertTrue(self.svc.check_formation(submitted["id"], "airspace_reviewer", "")["approvable"])

    def test_rescue_preempt_displaces_and_reinstatement_keeps_basis(self):
        plans, formation = self.build_team(("R1", "R2", "R3", "R4"), slots=4, name="常规巡检队")
        self.submit_team(plans, formation)
        self.approve_team(formation["id"], revision=2, offline="fa-r-1")
        basis_before = self.svc.get_formation(formation["id"], "airspace_reviewer", "")["basis_approval"]["id"]

        rescue_plans = [self.plan(c, route=[[118.20, 31.82], [118.28, 31.88]]) for c in ("X1", "X2")]
        rescue = self.svc.create_formation("op-user", "operator", "OP1", {
            "name": "救援队", "plan_ids": [p["id"] for p in rescue_plans], "leader_plan_id": rescue_plans[0]["id"]})
        self.submit_team(rescue_plans, rescue)
        # 审核员不能顶出
        with self.assertRaises(ApiError) as ctx:
            self.approve_team(rescue["id"], revision=2, offline="fa-r-rescue-deny", preempt=True, override_reason="救援")
        self.assertEqual(ctx.exception.code, "preempt_forbidden")
        # 普通指挥官批准不抢占 -> capacity_full
        with self.assertRaises(ApiError) as ctx:
            self.approve_team(rescue["id"], revision=2, offline="fa-r-rescue-full", role="commander")
        self.assertEqual(ctx.exception.code, "capacity_full")

        result = self.approve_team(rescue["id"], revision=2, offline="fa-r-rescue", reviewer="commander",
                                   role="commander", reason="灾区侦察", override_reason="应急救援授权", preempt=True)
        self.assertEqual(result["override_kind"], "rescue_preempt")
        self.assertEqual(result["displaced_formation_ids"], [formation["id"]])
        victim = self.svc.get_formation(formation["id"], "airspace_reviewer", "")
        self.assertEqual(victim["status"], "displaced")
        self.assertEqual(victim["displaced_by_formation_id"], rescue["id"])
        # 原批准依据保留
        self.assertEqual(victim["basis_approval"]["id"], basis_before)
        for member in victim["members"]:
            self.assertEqual(member["status"], "displaced")

        # 顶出期间成员一旦改动，原依据失效，不能复队
        self.svc.change(plans[0]["id"], "op-user", "operator", "OP1", {
            "expected_revision": 1, "route": [[116.42, 39.82], [116.62, 39.92]]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reinstate_formation(formation["id"], "reviewer", "airspace_reviewer", {"reason": "请求复队"})
        self.assertEqual(ctx.exception.code, "basis_stale")

    def test_reinstatement_succeeds_when_capacity_frees(self):
        plans, formation = self.build_team(("P1", "P2"), slots=2, name="被顶出队")
        self.submit_team(plans, formation)
        self.approve_team(formation["id"], revision=2, offline="fa-p-1")
        rescue_plans = [self.plan("Q1", route=[[118.21, 31.82], [118.27, 31.87]])]
        rescue = self.svc.create_formation("op-user", "operator", "OP1", {
            "name": "救援队Q", "plan_ids": [p["id"] for p in rescue_plans], "leader_plan_id": rescue_plans[0]["id"]})
        self.submit_team(rescue_plans, rescue)
        self.approve_team(rescue["id"], revision=2, offline="fa-p-rescue", reviewer="commander", role="commander",
                          reason="救援", override_reason="应急授权", preempt=True)
        self.assertEqual(self.svc.get_formation(formation["id"], "operator", "OP1")["status"], "displaced")
        # 救援窗口提前结束并到期释放名额
        self.svc.repo.conn.execute("UPDATE flight_plans SET ends_at=? WHERE id=?", (iso(utcnow() - timedelta(minutes=1)), rescue_plans[0]["id"]))
        self.svc.expire_plans("reviewer", "airspace_reviewer")
        # 被顶出队凭原批准依据复队
        result = self.svc.reinstate_formation(formation["id"], "reviewer", "airspace_reviewer", {"reason": "空域已腾退"})
        self.assertEqual(result["formation"]["status"], "approved")
        self.assertIsNotNone(result["basis_approval_id"])
        held = self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"]
        self.assertEqual(held, 2)

    def test_dissolve_releases_and_leader_cannot_cancel_directly(self):
        plans, formation = self.build_team(("L1", "L2"), slots=2, name="待解散")
        self.submit_team(plans, formation)
        self.approve_team(formation["id"], revision=2, offline="fa-l-1")
        with self.assertRaises(ApiError) as ctx:
            self.svc.cancel(plans[0]["id"], "op-user", "operator", "OP1", {"reason": "队长想退出"})
        self.assertEqual(ctx.exception.code, "leader_cannot_leave")
        out = self.svc.dissolve_formation(formation["id"], "op-user", "operator", "OP1", {"reason": "任务取消"})
        self.assertTrue(out["released_capacity"])
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) c FROM formation_slot_occupancy WHERE status='held'").fetchone()["c"], 0)
        for p in plans:
            self.assertEqual(self.svc.get_plan(p["id"], "operator", "OP1")["status"], "draft")


if __name__ == "__main__":
    unittest.main()
