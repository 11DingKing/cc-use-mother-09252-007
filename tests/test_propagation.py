"""传播计算：依赖环、菱形多依据、版本化规则、撤销与派生风险关闭。"""
import unittest

import helpers


class PropagationTests(helpers.ServiceTestCase):
    def test_cycle_terminates_without_duplicate_risks(self):
        helpers.build_graph(self.svc)
        result = self.svc.import_events([
            helpers.make_event("E1", "A", "visa", "critical")])[0]
        self.assertEqual(result["status"], "imported")
        impacted = {i["project_id"]: i for i in result["impacts"]}
        # 环被截断：A 自身不会收到派生风险，且每个项目只出现一次
        self.assertEqual(set(impacted), {"B", "C", "D"})
        self.assertEqual(impacted["B"]["severity"], "high")    # 衰减 1 档
        self.assertEqual(impacted["C"]["severity"], "medium")  # 衰减 2 档
        self.assertEqual(impacted["D"]["severity"], "high")    # A->D 直达优先于 A->B->D
        self.assertEqual(impacted["D"]["path"], ["A", "D"])
        # 时限按落点地区工作日历
        self.assertEqual(impacted["B"]["deadline"], "2026-10-05")  # GULF
        self.assertEqual(impacted["D"]["deadline"], "2026-10-13")  # CN
        self.assertEqual(impacted["C"]["deadline"], "2026-10-23")  # CN，medium 15 天
        # 重复导入结果一致，不产生重复风险
        again = self.svc.import_events([
            helpers.make_event("E1", "A", "visa", "critical")])[0]
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(len(self.svc.dump_state()["risks"]), 4)  # 1 基础 + 3 派生

    def test_cycle_back_edge_still_propagates_to_other_members(self):
        # 在环中间的 C 上发生事件，应沿 C->A->B 传播，C 自身不受影响
        helpers.build_graph(self.svc)
        result = self.svc.import_events([
            helpers.make_event("E9", "C", "visa", "high")])[0]
        impacted = {i["project_id"]: i for i in result["impacts"]}
        self.assertEqual(set(impacted), {"A", "B", "D"})
        self.assertEqual(impacted["A"]["severity"], "medium")
        self.assertEqual(impacted["B"]["severity"], "low")

    def test_diamond_gives_multiple_bases_and_partial_revoke_keeps_open(self):
        helpers.build_graph(self.svc)
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])
        self.svc.import_events([helpers.make_event("E2", "B", "visa", "high")])
        derived = self.svc.get_risk("D:visa:D")
        self.assertEqual({b["event_id"] for b in derived["bases"]}, {"E1", "E2"})
        self.assertTrue(all(b["active"] for b in derived["bases"]))
        # 撤销 E1：D 仍有独立依据 E2，保持打开，级别按 E2 路径重算
        self.svc.revoke_event("E1", "签证政策取消")
        derived = self.svc.get_risk("D:visa:D")
        self.assertEqual(derived["status"], "open")
        active = [b for b in derived["bases"] if b["active"]]
        self.assertEqual([b["event_id"] for b in active], ["E2"])
        self.assertEqual(derived["severity"], "medium")  # B->D 衰减一档
        # 撤销 E2：D 失去全部依据，自动关闭
        self.svc.revoke_event("E2", "跟进后排除")
        derived = self.svc.get_risk("D:visa:D")
        self.assertEqual(derived["status"], "closed")
        self.assertEqual(derived["close_reason"], "no_active_basis")

    def test_revoke_closes_whole_tree_when_no_independent_basis(self):
        helpers.build_graph(self.svc)
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "critical")])
        self.svc.revoke_event("E1", "误报")
        for rid in ("B:E1", "D:visa:B", "D:visa:C", "D:visa:D"):
            self.assertEqual(self.svc.get_risk(rid)["status"], "closed", rid)
        self.assertEqual(self.svc.get_risk("B:E1")["close_reason"], "revoked")

    def test_new_basis_reopens_closed_derived_risk(self):
        helpers.build_graph(self.svc)
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])
        self.svc.revoke_event("E1", "撤销")
        self.assertEqual(self.svc.get_risk("D:visa:B")["status"], "closed")
        self.svc.import_events([helpers.make_event("E2", "A", "visa", "medium")])
        derived = self.svc.get_risk("D:visa:B")
        self.assertEqual(derived["status"], "open")
        self.assertEqual([b["event_id"] for b in derived["bases"] if b["active"]], ["E2"])

    def test_resolving_base_closes_derived_without_independent_basis(self):
        helpers.build_graph(self.svc)
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])
        self.svc.import_events([helpers.make_event("E2", "B", "visa", "high")])
        self.svc.resolve_risk("B:E1", actor="张老师", note="签证已补办")
        # D 仍有 E2 依据，保持打开
        self.assertEqual(self.svc.get_risk("D:visa:D")["status"], "open")
        # B 的派生风险只有 E1 一条依据，随解决关闭
        self.assertEqual(self.svc.get_risk("D:visa:B")["status"], "closed")
        self.svc.resolve_risk("B:E2", actor="张老师")
        self.assertEqual(self.svc.get_risk("D:visa:D")["status"], "closed")

    def test_rule_versions_are_pinned_per_risk(self):
        helpers.build_graph(self.svc)
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "critical")])
        self.assertEqual(self.svc.get_risk("D:visa:B")["rule_version"], "v1")
        # 新版本：衰减 2 档
        self.svc.create_project("E", "艾普西龙项目", "CN")
        self.svc.add_dependency("A", "E", "visa")
        rules_v2 = {
            "propagation": {
                "visa": {"edge_types": ["visa", "campus"], "max_depth": 4,
                         "decay_per_hop": 2},
                "standard": {"edge_types": ["standard"], "max_depth": 3,
                             "decay_per_hop": 1},
                "faculty": {"edge_types": ["faculty"], "max_depth": 2,
                            "decay_per_hop": 1},
            },
            "deadline_workdays": {"critical": 2, "high": 5, "medium": 10, "low": 20},
            "auto_escalate_overdue": True,
        }
        self.svc.register_rules("v2", rules_v2)
        self.assertEqual(self.svc.active_rules()["version"], "v2")
        result = self.svc.import_events([
            helpers.make_event("E2", "A", "visa", "critical")])[0]
        impacted = {i["project_id"]: i for i in result["impacts"]}
        # 衰减 2 档：B/D/E 为 medium，C 衰减到 0 不再出现
        self.assertEqual(set(impacted), {"B", "D", "E"})
        self.assertEqual(impacted["E"]["severity"], "medium")
        # 新风险按 v2 计算并固化版本；既有风险保持 v1
        self.assertEqual(self.svc.get_risk("D:visa:E")["rule_version"], "v2")
        self.assertEqual(self.svc.get_risk("D:visa:B")["rule_version"], "v1")
        # v2 时限口径：medium 10 个 CN 工作日（周五起，跨国庆）
        self.assertEqual(impacted["E"]["deadline"], "2026-10-16")

    def test_category_follows_only_matching_edge_types(self):
        helpers.build_graph(self.svc)
        # 师资事件不应沿 visa 边传播
        result = self.svc.import_events([
            helpers.make_event("E3", "A", "faculty", "critical")])[0]
        self.assertEqual(result["impacts"], [])
        self.assertEqual(len(self.svc.dump_state()["risks"]), 1)


if __name__ == "__main__":
    unittest.main()
