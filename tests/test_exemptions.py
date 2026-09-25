"""人工确认豁免：明确有效期，到期自动恢复暴露。"""
from __future__ import annotations

import unittest

import helpers
from service_09252_007.errors import NotFoundError, ValidationError


class ExemptionTests(helpers.ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.add_project("A")
        # 严重度 5 → 2 个工作日，2026-09-25（周五）发生 → 截止 2026-09-29（周二）
        self.raise_risk("R1", "A", severity=5, occurred_at="2026-09-25T09:00:00+00:00")

    def test_grant_and_expiry(self):
        ex = self.service.grant_exemption(
            "R1",
            approved_by="director-1",
            reason="使团加急通道已确认",
            valid_from="2026-09-25T00:00:00+00:00",
            valid_until="2026-09-30T12:00:00+00:00",
        )
        risk = self.service.get_risk("R1")
        self.assertEqual(risk["effective_status"], "exempt")
        self.assertEqual(risk["exemption"]["exemption_id"], ex["exemption_id"])
        self.assertEqual(risk["exemption"]["valid_until"], "2026-09-30T12:00:00.000000+00:00")

        # 豁免期内即使越过处置时限也不计逾期
        self.clock.advance(days=5)  # 2026-09-30 09:00，截止日 09-29 已过
        risk = self.service.get_risk("R1")
        self.assertEqual(risk["effective_status"], "exempt")
        self.assertFalse(risk["overdue"])
        self.assertEqual(self.service.overdue_risks(), [])

        # 有效期过后恢复暴露，并进入逾期
        self.clock.advance(hours=4)  # 2026-09-30 13:00，豁免已失效
        risk = self.service.get_risk("R1")
        self.assertEqual(risk["effective_status"], "open")
        self.assertTrue(risk["overdue"])
        self.assertEqual([r["risk_key"] for r in self.service.overdue_risks()], ["R1"])

    def test_revoke_exemption(self):
        ex = self.service.grant_exemption(
            "R1", "director-1", "临时豁免",
            "2026-09-25T00:00:00+00:00", "2026-10-25T00:00:00+00:00",
        )
        self.service.revoke_exemption(ex["exemption_id"], actor="director-1")
        self.assertEqual(self.service.get_risk("R1")["effective_status"], "open")
        # 重复撤销幂等
        again = self.service.revoke_exemption(ex["exemption_id"], actor="director-1")
        self.assertIsNotNone(again["revoked_at"])

    def test_validity_window_required(self):
        with self.assertRaises(ValidationError):
            self.service.grant_exemption(
                "R1", "director-1", "有效期倒置",
                "2026-09-25T00:00:00+00:00", "2026-09-24T00:00:00+00:00",
            )
        with self.assertRaises(NotFoundError):
            self.service.grant_exemption(
                "no-such-risk", "director-1", "x",
                "2026-09-25T00:00:00+00:00", "2026-09-27T00:00:00+00:00",
            )

    def test_exemption_on_derived_risk(self):
        self.add_project("B")
        self.add_dependency("D1", "A", "B")
        derived_key = "derived:B:visa"
        self.service.grant_exemption(
            derived_key, "director-1", "下游批次已顺延",
            "2026-09-25T00:00:00+00:00", "2026-10-25T00:00:00+00:00",
        )
        self.assertEqual(self.service.get_risk(derived_key)["effective_status"], "exempt")
        active = self.service.list_exemptions(active_only=True)
        self.assertEqual([e["risk_key"] for e in active], [derived_key])


if __name__ == "__main__":
    unittest.main()
