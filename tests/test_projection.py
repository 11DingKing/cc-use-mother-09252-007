"""传播投影：依赖环、撤销语义、乱序/重复导入、版本化规则。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers
from service_09252_007.projection import derived_key
from service_09252_007.service import RiskService

T1 = "2026-09-25T09:00:00+00:00"
T2 = "2026-09-26T09:00:00+00:00"
T3 = "2026-09-27T09:00:00+00:00"


class PropagationTests(helpers.ServiceTestCase):
    def test_linear_propagation_with_attenuation(self):
        for p in ("A", "B", "C"):
            self.add_project(p)
        self.add_dependency("D1", "A", "B")
        self.add_dependency("D2", "B", "C")
        self.raise_risk("R1", "A", severity=5, occurred_at=T1)

        base = self.service.get_risk("R1")
        self.assertEqual((base["kind"], base["severity"], base["status"]), ("base", 5, "open"))

        mid = self.service.get_risk(derived_key("B", "visa"))
        self.assertEqual(mid["severity"], 4)  # 每跳衰减 1
        self.assertEqual([e["base_risk_id"] for e in mid["basis"]], ["R1"])
        self.assertEqual(mid["basis"][0]["hops"], 1)

        far = self.service.get_risk(derived_key("C", "visa"))
        self.assertEqual(far["severity"], 3)
        self.assertEqual(far["basis"][0]["hops"], 2)

        propagation = self.service.risk_propagation("R1")
        self.assertEqual(
            [a["project_id"] for a in propagation["affected_projects"]], ["B", "C"]
        )

    def test_dependency_cycle_terminates(self):
        for p in ("A", "B", "C"):
            self.add_project(p)
        self.add_dependency("D1", "A", "B")
        self.add_dependency("D2", "B", "C")
        self.add_dependency("D3", "C", "A")  # 环
        self.add_dependency("D4", "A", "A")  # 自环
        self.raise_risk("R1", "A", severity=5, occurred_at=T1)

        keys = {r["risk_key"] for r in self.service.list_risks()}
        # 环回到 A 时 A 已在 visited 集合中，不会生成 A 的派生风险
        self.assertEqual(keys, {"R1", derived_key("B", "visa"), derived_key("C", "visa")})

    def test_revoke_closes_only_derived_without_independent_basis(self):
        for p in ("P1", "P2", "Q"):
            self.add_project(p)
        self.add_dependency("D1", "P1", "Q")
        self.add_dependency("D2", "P2", "Q")
        self.raise_risk("B1", "P1", severity=5, occurred_at=T1)
        self.raise_risk("B2", "P2", severity=4, occurred_at=T2)

        derived = self.service.get_risk(derived_key("Q", "visa"))
        self.assertEqual(derived["severity"], 4)  # max(5-1, 4-1)
        self.assertEqual({e["base_risk_id"] for e in derived["basis"]}, {"B1", "B2"})

        # 撤销 B1：Q 的派生风险仍有 B2 作为独立依据，保持打开
        self.service.revoke_risk("B1", reason="签证恢复", occurred_at=T3)
        derived = self.service.get_risk(derived_key("Q", "visa"))
        self.assertEqual(derived["status"], "open")
        self.assertEqual(derived["severity"], 3)
        self.assertEqual([e["base_risk_id"] for e in derived["basis"]], ["B2"])
        self.assertEqual(self.service.get_risk("B1")["status"], "closed")
        self.assertEqual(self.service.get_risk("B1")["close_reason"], "revoked")

        # 撤销 B2：派生风险失去全部依据，被关闭
        self.service.revoke_risk("B2", occurred_at="2026-09-28T09:00:00+00:00")
        derived = self.service.get_risk(derived_key("Q", "visa"))
        self.assertEqual(derived["status"], "closed")
        self.assertEqual(derived["close_reason"], "basis_empty")
        self.assertEqual(derived["basis"], [])

    def test_dependency_removal_closes_derived(self):
        self.add_project("A")
        self.add_project("B")
        self.add_dependency("D1", "A", "B")
        self.raise_risk("R1", "A", occurred_at=T1)
        self.assertEqual(self.service.get_risk(derived_key("B", "visa"))["status"], "open")

        self.service.remove_dependency("D1")
        derived = self.service.get_risk(derived_key("B", "visa"))
        self.assertEqual(derived["status"], "closed")
        self.assertEqual(derived["close_reason"], "basis_empty")

    def test_derived_reopens_when_basis_returns(self):
        self.add_project("A")
        self.add_project("B")
        self.add_dependency("D1", "A", "B")
        self.raise_risk("R1", "A", occurred_at=T1)
        self.service.revoke_risk("R1", occurred_at=T2)
        self.assertEqual(self.service.get_risk(derived_key("B", "visa"))["status"], "closed")

        self.raise_risk("R2", "A", occurred_at=T3)
        derived = self.service.get_risk(derived_key("B", "visa"))
        self.assertEqual(derived["status"], "open")
        self.assertEqual([e["base_risk_id"] for e in derived["basis"]], ["R2"])
        actions = [a["action"] for a in self.service.list_audit(entity_id=derived_key("B", "visa"))]
        self.assertIn("risk_reopened", actions)


class OrderingTests(helpers.ServiceTestCase):
    """乱序与重复消息：最终状态与顺序无关。"""

    def _events(self):
        raise_event = {
            "message_id": "m-raise",
            "event_type": "risk_raised",
            "occurred_at": T1,
            "payload": {"risk_id": "R1", "project_id": "A", "category": "visa", "severity": 5},
        }
        revoke_event = {
            "message_id": "m-revoke",
            "event_type": "risk_revoked",
            "occurred_at": T2,
            "payload": {"risk_id": "R1", "reason": "恢复"},
        }
        return raise_event, revoke_event

    def test_out_of_order_delivery_converges(self):
        raise_event, revoke_event = self._events()

        # 顺序一：先发生后撤销
        self.add_project("A")
        self.service.import_events([raise_event, revoke_event])
        ordered = self.service.risks_dump()

        # 顺序二：撤销消息先到达（独立服务实例）
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        other = RiskService(Path(tmp.name) / "risk.db", clock=self.clock)
        self.addCleanup(other.close)
        other.register_project("A", "项目A", "default", occurred_at="2026-09-01T00:00:00+00:00")
        other.import_events([revoke_event])  # 撤销先到：风险尚不存在，重放时按发生时间落位
        other.import_events([raise_event])

        self.assertEqual(ordered, other.risks_dump())
        self.assertEqual(other.get_risk("R1")["status"], "closed")
        self.assertEqual(other.get_risk("R1")["close_reason"], "revoked")

    def test_duplicate_import_is_idempotent(self):
        self.add_project("A")
        self.add_project("B")
        self.add_dependency("D1", "A", "B")
        raise_event, _ = self._events()

        first = self.service.import_events([raise_event])
        self.assertEqual(first["imported"], 1)
        snapshot = self.service.risks_dump()
        audit_len = len(self.service.list_audit())

        second = self.service.import_events([raise_event])
        self.assertEqual((second["imported"], second["duplicates"]), (0, 1))
        self.assertEqual(snapshot, self.service.risks_dump())
        self.assertEqual(audit_len, len(self.service.list_audit()))  # 不产生重复审计

    def test_same_risk_id_different_message_dedupes(self):
        self.add_project("A")
        event = {
            "message_id": "m-1",
            "event_type": "risk_raised",
            "occurred_at": T1,
            "payload": {"risk_id": "R1", "project_id": "A", "category": "visa", "severity": 5},
        }
        replay = dict(event, message_id="m-2")  # 同一 risk_id 的重复上报
        result = self.service.import_events([event, replay])
        self.assertEqual(result["imported"], 2)  # 两条消息都入库
        self.assertEqual(len(self.service.list_risks()), 1)  # 但只形成一个风险

    def test_invalid_event_rejected_atomically(self):
        self.add_project("A")
        good, _ = self._events()
        bad = {"message_id": "m-bad", "event_type": "risk_raised", "payload": {"risk_id": "R2"}}
        with self.assertRaises(Exception) as ctx:
            self.service.import_events([good, bad])
        self.assertIn("非法", str(ctx.exception))
        self.assertEqual(self.service.list_risks(), [])  # 整批拒绝，无部分导入


class VersionedRuleTests(helpers.ServiceTestCase):
    def test_rules_pin_propagation_by_raise_time(self):
        for p in ("A", "B", "C", "D"):
            self.add_project(p)
        self.add_dependency("D1", "A", "B")
        self.add_dependency("D2", "B", "C")
        self.add_dependency("D3", "C", "D")

        self.service.publish_rule(
            {
                "version": 1,
                "effective_from": "2026-01-01T00:00:00+00:00",
                "max_depth": 1,
                "severity_decay_per_hop": 1,
                "min_propagated_severity": 1,
                "propagating_dependency_kinds": ["shared-faculty"],
                "deadline_workdays": {"5": 2, "4": 3, "3": 5, "2": 8, "1": 13},
            }
        )
        self.raise_risk("R1", "A", category="visa", severity=5,
                        occurred_at="2026-02-01T09:00:00+00:00")
        affected = self.service.risk_propagation("R1")["affected_projects"]
        self.assertEqual([a["project_id"] for a in affected], ["B"])  # v1 只传一跳

        self.service.publish_rule(
            {
                "version": 2,
                "effective_from": "2026-03-01T00:00:00+00:00",
                "max_depth": 3,
                "severity_decay_per_hop": 1,
                "min_propagated_severity": 1,
                "propagating_dependency_kinds": ["shared-faculty"],
                "deadline_workdays": {"5": 2, "4": 3, "3": 5, "2": 8, "1": 13},
            }
        )
        self.raise_risk("R2", "A", category="standard", severity=5,
                        occurred_at="2026-04-01T09:00:00+00:00")
        affected2 = self.service.risk_propagation("R2")["affected_projects"]
        self.assertEqual([a["project_id"] for a in affected2], ["B", "C", "D"])

        # R1 仍按 v1 计算，传播范围与规则版本不变
        affected1 = self.service.risk_propagation("R1")["affected_projects"]
        self.assertEqual([a["project_id"] for a in affected1], ["B"])
        self.assertEqual(self.service.get_risk("R1")["rule_version"], 1)
        self.assertEqual(self.service.get_risk("R2")["rule_version"], 2)

    def test_dependency_kind_filtering(self):
        self.add_project("A")
        self.add_project("B")
        self.add_project("C")
        self.add_dependency("D1", "A", "B", kind="shared-faculty")
        self.add_dependency("D2", "A", "C", kind="marketing")  # 不在传播白名单
        self.raise_risk("R1", "A", occurred_at=T1)

        keys = {r["risk_key"] for r in self.service.list_risks()}
        self.assertEqual(keys, {"R1", derived_key("B", "visa")})


if __name__ == "__main__":
    unittest.main()
