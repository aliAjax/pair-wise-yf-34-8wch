import sys, tempfile, threading, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class FormationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.svc = DroneAirspaceService(self.db, formation_capacity=2)
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, callsign, route, operator="OP1"):
        return self.svc.create_plan("op-user", "operator", operator, {
            "callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route, "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)),
            "max_altitude": 100, "population_risk": 1, "emergency_plan": "返回起降点", "region": "BJ"})

    def formation(self, name, plan_ids, kind="standard"):
        return self.svc.create_formation("op-user", "operator", "OP1",
                                         {"name": name, "kind": kind, "member_plan_ids": plan_ids})

    def submit_approve(self, formation_id, offline_id, role="airspace_reviewer", override=""):
        self.svc.submit_formation(formation_id, "op-user", "operator", "OP1")
        return self.svc.approve_formation(formation_id, "rev", role,
                                          {"expected_revision": 1, "offline_id": offline_id,
                                           "reason": "整队满足要求", "override_reason": override})

    def test_formation_binds_members_and_occupies_capacity_once(self):
        p1 = self.plan("D100", [[116.1, 39.8], [116.2, 39.9]])
        p2 = self.plan("D101", [[116.105, 39.805], [116.205, 39.905]])
        f = self.formation("巡检一队", [p1["id"], p2["id"]])
        self.assertEqual(f["status"], "forming")
        self.assertEqual([m["callsign"] for m in f["members"]], ["D100", "D101"])
        self.assertEqual(f["leader_plan_id"], p1["id"])
        result = self.submit_approve(f["id"], "off-f1")
        self.assertEqual(result["formation"]["status"], "approved")
        for m in result["formation"]["members"]:
            self.assertEqual(m["plan_status"], "approved")
        # 两架飞机只占一个容量：第二个重叠编队看到 used=1
        p3 = self.plan("D102", [[116.11, 39.81], [116.21, 39.91]])
        f2 = self.formation("巡检二队", [p3["id"]])
        self.svc.submit_formation(f2["id"], "op-user", "operator", "OP1")
        check = self.svc._formation_conflict_check(self.svc.repo.conn, f2["id"])
        self.assertEqual(check["used"], 1)
        self.assertEqual(check["remaining_capacity"], 1)

    def test_capacity_conflict_reports_conflicting_member_and_remaining(self):
        p1 = self.plan("D200", [[116.1, 39.8], [116.2, 39.9]])
        p2 = self.plan("D201", [[116.11, 39.81], [116.21, 39.91]])
        p3 = self.plan("D202", [[116.12, 39.82], [116.22, 39.92]])
        f1 = self.formation("一队", [p1["id"]])
        f2 = self.formation("二队", [p2["id"]])
        f3 = self.formation("三队", [p3["id"]])
        self.submit_approve(f1["id"], "off-c1")
        self.submit_approve(f2["id"], "off-c2")
        self.svc.submit_formation(f3["id"], "op-user", "operator", "OP1")
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve_formation(f3["id"], "rev", "airspace_reviewer",
                                       {"expected_revision": 1, "offline_id": "off-c3", "reason": "复核"})
        self.assertEqual(ctx.exception.code, "formation_capacity_conflict")
        details = ctx.exception.details
        self.assertEqual(details["remaining_capacity"], 0)
        self.assertEqual(details["used"], 2)
        names = {c["name"]: [m["callsign"] for m in c["conflicting_members"]] for c in details["conflicting_formations"]}
        self.assertIn("一队", names)
        self.assertIn("二队", names)
        self.assertIn("D200", names["一队"])

    def test_member_time_or_route_change_invalidates_whole_formation(self):
        p1 = self.plan("D300", [[116.1, 39.8], [116.2, 39.9]])
        p2 = self.plan("D301", [[116.105, 39.805], [116.205, 39.905]])
        f = self.formation("巡检二队", [p1["id"], p2["id"]])
        self.submit_approve(f["id"], "off-i1")
        # 队长改一架航线
        changed = self.svc.change(p1["id"], "op-user", "operator", "OP1",
                                  {"expected_revision": 1, "route": [[116.5, 39.5], [116.6, 39.6]]})
        self.assertEqual(changed["status"], "draft")
        after = self.svc.get_formation(f["id"], "airspace_reviewer", "")
        self.assertEqual(after["status"], "invalidated")
        statuses = {m["callsign"]: m["plan_status"] for m in after["members"]}
        self.assertEqual(statuses["D300"], "draft")
        self.assertEqual(statuses["D301"], "submitted")
        # 旧占用释放：新编队可以批准
        p3 = self.plan("D302", [[116.13, 39.83], [116.23, 39.93]])
        f2 = self.formation("补位队", [p3["id"]])
        self.submit_approve(f2["id"], "off-i2")
        self.assertEqual(self.svc.get_formation(f2["id"], "airspace_reviewer", "")["status"], "approved")

    def test_concurrent_dispatchers_only_one_fills_quota(self):
        p1 = self.plan("D400", [[116.1, 39.8], [116.2, 39.9]])
        fx = self.formation("X队", [p1["id"]])
        self.submit_approve(fx["id"], "off-x")
        # 容量 2，已占 1，剩 1 个名额
        py = self.plan("D401", [[116.12, 39.82], [116.22, 39.92]])
        pz = self.plan("D402", [[116.13, 39.83], [116.23, 39.93]])
        fy = self.formation("Y队", [py["id"]])
        fz = self.formation("Z队", [pz["id"]])
        self.svc.submit_formation(fy["id"], "op-user", "operator", "OP1")
        self.svc.submit_formation(fz["id"], "op-user", "operator", "OP1")
        outcomes = {}

        def approve(fid, offline):
            svc = DroneAirspaceService(self.db, formation_capacity=2)  # 每线程独立连接
            try:
                res = svc.approve_formation(fid, "rev", "airspace_reviewer",
                                            {"expected_revision": 1, "offline_id": offline, "reason": "抢占名额"})
                outcomes[offline] = ("approved", res["formation"]["status"])
            except ApiError as exc:
                outcomes[offline] = (exc.code, exc.details)

        t1 = threading.Thread(target=approve, args=(fy["id"], "off-y"))
        t2 = threading.Thread(target=approve, args=(fz["id"], "off-z"))
        t1.start(); t2.start(); t1.join(); t2.join()
        winners = [v for v in outcomes.values() if v[0] == "approved"]
        losers = [v for v in outcomes.values() if v[0] == "formation_capacity_conflict"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        loser = losers[0]
        self.assertEqual(loser[1]["remaining_capacity"], 0)
        self.assertTrue(any(c["name"] in {"Y队", "Z队"} for c in loser[1]["conflicting_formations"]))

    def test_split_approved_formation_rechecks_conflicts(self):
        p1 = self.plan("D500", [[116.1, 39.8], [116.2, 39.9]])
        p2 = self.plan("D501", [[116.105, 39.805], [116.205, 39.905]])
        f = self.formation("巡检三队", [p1["id"], p2["id"]])
        self.submit_approve(f["id"], "off-s1")
        result = self.svc.split_formation(f["id"], "op-user", "operator", "OP1",
                                          {"name": "拆出小队", "member_plan_ids": [p2["id"]]})
        self.assertEqual(result["old_formation"]["status"], "invalidated")
        self.assertEqual(result["new_formation"]["status"], "forming")
        self.assertEqual([m["callsign"] for m in result["new_formation"]["members"]], ["D501"])
        self.assertIn("approvable", result["new_conflicts"])
        # 新编队需重新提交批准
        self.svc.submit_formation(result["new_formation"]["id"], "op-user", "operator", "OP1")
        approved = self.svc.approve_formation(result["new_formation"]["id"], "rev", "airspace_reviewer",
                                             {"expected_revision": 1, "offline_id": "off-s2", "reason": "重新核对后批准"})
        self.assertEqual(approved["formation"]["status"], "approved")

    def test_merge_formations_rechecks_conflicts(self):
        p1 = self.plan("D600", [[116.1, 39.8], [116.2, 39.9]])
        p2 = self.plan("D601", [[116.11, 39.81], [116.21, 39.91]])
        f1 = self.formation("五队", [p1["id"]])
        f2 = self.formation("六队", [p2["id"]])
        result = self.svc.merge_formations("op-user", "operator", "OP1",
                                           {"name": "合并大队", "formation_ids": [f1["id"], f2["id"]]})
        self.assertEqual(result["formation"]["status"], "forming")
        self.assertEqual([m["callsign"] for m in result["formation"]["members"]], ["D600", "D601"])
        self.assertIn("approvable", result["conflicts"])
        self.assertEqual(self.svc.get_formation(f1["id"], "airspace_reviewer", "")["status"], "dissolved")
        self.assertEqual(self.svc.get_formation(f2["id"], "airspace_reviewer", "")["status"], "dissolved")

    def test_rescue_insertion_bumps_formation_retains_approval_basis(self):
        p1 = self.plan("D700", [[116.1, 39.8], [116.2, 39.9]])
        p2 = self.plan("D701", [[116.11, 39.81], [116.21, 39.91]])
        f1 = self.formation("一队", [p1["id"]])
        f2 = self.formation("二队", [p2["id"]])
        self.submit_approve(f1["id"], "off-r1")
        self.submit_approve(f2["id"], "off-r2")
        # 容量已满，救援队插队
        pr = self.plan("D702", [[116.115, 39.815], [116.215, 39.915]])
        rescue = self.formation("救援队", [pr["id"]], kind="rescue")
        self.svc.submit_formation(rescue["id"], "op-user", "operator", "OP1")
        result = self.svc.approve_formation(rescue["id"], "cmd", "commander",
                                            {"expected_revision": 1, "offline_id": "off-rescue",
                                             "reason": "紧急救援", "override_reason": "应急救援授权"})
        self.assertEqual(result["formation"]["status"], "approved")
        self.assertEqual(len(result["bumped_formations"]), 1)
        bumped = result["bumped_formations"][0]
        self.assertEqual([m["callsign"] for m in bumped["conflicting_members"]], ["D701"])
        # 被顶出编队保留原批准依据
        bumped_row = self.svc.get_formation(bumped["formation_id"], "airspace_reviewer", "")
        self.assertEqual(bumped_row["status"], "bumped")
        self.assertEqual(bumped_row["bumped_by_formation_id"], rescue["id"])
        for m in bumped_row["members"]:
            plan = self.svc.get_plan(m["plan_id"], "airspace_reviewer", "")
            self.assertEqual(plan["status"], "approved")
            self.assertEqual(len(plan["approvals"]), 1)
            self.assertEqual(plan["approvals"][0]["decision"], "approved")
        # 容量占满时不能恢复
        with self.assertRaises(ApiError) as ctx:
            self.svc.reinstate_formation(bumped["formation_id"], "cmd", "commander", {})
        self.assertEqual(ctx.exception.code, "formation_capacity_conflict")
        # 救援队任务结束（取消成员）后腾出名额，被顶出编队可恢复且依据仍在
        self.svc.cancel(pr["id"], "cmd", "commander", "airspace_reviewer", {"reason": "救援结束"})
        reinstated = self.svc.reinstate_formation(bumped["formation_id"], "cmd", "commander", {})
        self.assertEqual(reinstated["formation"]["status"], "approved")
        for m in reinstated["formation"]["members"]:
            self.assertEqual(m["plan_status"], "approved")


if __name__ == "__main__":
    unittest.main()
