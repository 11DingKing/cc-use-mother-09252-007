"""升级与复盘：时限收紧、逾期自动升级、复盘前置条件与唯一性。"""
import unittest

from service_09252_007.errors import Conflict, InvalidState

import helpers


class EscalationTests(helpers.ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.svc.create_project("A", "阿尔法项目", "CN")
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "medium")])

    def test_manual_escalation_tightens_deadline(self):
        base = self.svc.get_risk("B:E1")
        self.assertEqual(base["deadline"], "2026-10-23")  # medium 15 个 CN 工作日
        view = self.svc.escalate("B:E1", esc_id="ESC-1", actor="李主任",
                                 to_severity="high", reason="影响扩大")
        self.assertEqual(view["severity"], "high")
        self.assertEqual(view["deadline"], "2026-10-13")  # high 7 个工作日，收紧
        self.assertEqual(view["escalations"][0]["to_severity"], "high")

    def test_auto_bump_and_max_severity_guard(self):
        view = self.svc.escalate("B:E1", esc_id="ESC-1", actor="李主任")
        self.assertEqual(view["severity"], "high")
        view = self.svc.escalate("B:E1", esc_id="ESC-2", actor="李主任")
        self.assertEqual(view["severity"], "critical")
        with self.assertRaises(InvalidState):
            self.svc.escalate("B:E1", esc_id="ESC-3", actor="李主任")

    def test_sweep_auto_escalates_overdue_progressively(self):
        self.clock.advance(days=30)  # 2026-10-25，已过 medium 时限
        first = self.svc.sweep()
        self.assertEqual(first["escalated"],
                         [{"risk_id": "B:E1", "to_severity": "high"}])
        self.assertEqual(self.svc.get_risk("B:E1")["severity"], "high")
        second = self.svc.sweep()
        self.assertEqual(second["escalated"],
                         [{"risk_id": "B:E1", "to_severity": "critical"}])
        third = self.svc.sweep()  # 已到顶，不再升级
        self.assertEqual(third["escalated"], [])
        # 自动升级幂等键固定，不产生重复记录
        self.assertEqual(len(self.svc.dump_state()["escalations"]), 2)

    def test_sweep_respects_exempted_and_resolved(self):
        self.svc.create_exemption(
            "EX1", confirmed_by="王主任", reason="批次合并",
            valid_from="2026-09-25T00:00:00+00:00",
            valid_until="2026-12-31T00:00:00+00:00", risk_id="B:E1")
        self.svc.resolve_risk("B:E1", actor="张老师")
        self.clock.advance(days=30)
        self.assertEqual(self.svc.sweep()["escalated"], [])

    def test_escalation_recorded_in_audit(self):
        self.svc.escalate("B:E1", esc_id="ESC-1", actor="李主任", to_severity="high")
        entries = self.svc.audit_log(entity="risk", entity_id="B:E1")
        self.assertIn("risk_escalated", [e["action"] for e in entries])


class PostmortemTests(helpers.ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.svc.create_project("A", "阿尔法项目", "CN")
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])

    def test_postmortem_requires_closed_risk(self):
        with self.assertRaises(InvalidState):
            self.svc.create_postmortem("B:E1", pm_id="PM1", root_cause="材料缺失")

    def test_postmortem_after_resolve_and_uniqueness(self):
        self.svc.resolve_risk("B:E1", actor="张老师", note="已补办")
        pm = self.svc.create_postmortem(
            "B:E1", pm_id="PM1", root_cause="签证材料缺失",
            timeline="09-25 发现 -> 09-28 补办 -> 09-30 复核",
            lessons="批次启动前 30 天核验签证材料", created_by="李主任")
        self.assertEqual(pm["risk_id"], "B:E1")
        self.assertEqual(self.svc.get_postmortem("B:E1")["pm_id"], "PM1")
        # 同 pm_id 幂等；不同 pm_id 冲突（每条风险一份复盘）
        again = self.svc.create_postmortem("B:E1", pm_id="PM1")
        self.assertEqual(again["pm_id"], "PM1")
        with self.assertRaises(Conflict):
            self.svc.create_postmortem("B:E1", pm_id="PM2", root_cause="另一个")

    def test_postmortem_survives_restart(self):
        self.svc.resolve_risk("B:E1", actor="张老师")
        self.svc.create_postmortem("B:E1", pm_id="PM1", root_cause="材料缺失")
        svc2 = self.new_service()
        self.assertEqual(svc2.get_postmortem("B:E1")["root_cause"], "材料缺失")


if __name__ == "__main__":
    unittest.main()
