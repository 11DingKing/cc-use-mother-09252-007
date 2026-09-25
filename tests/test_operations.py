"""升级与复盘。"""
from __future__ import annotations

import unittest

import helpers
from service_09252_007.errors import NotFoundError, ValidationError


class OperationTests(helpers.ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.add_project("A")
        self.raise_risk("R1", "A")

    def test_escalation_levels_increment(self):
        first = self.service.escalate("R1", "初判超时", "office-1")
        second = self.service.escalate("R1", "影响扩大", "office-1")
        self.assertEqual((first["level"], second["level"]), (1, 2))
        escalations = self.service.list_escalations(risk_key="R1")
        self.assertEqual(len(escalations), 2)
        with self.assertRaises(NotFoundError):
            self.service.escalate("no-such-risk", "x", "office-1")

    def test_explicit_level_validated(self):
        esc = self.service.escalate("R1", "直接提级", "office-1", level=3)
        self.assertEqual(esc["level"], 3)
        with self.assertRaises(ValidationError):
            self.service.escalate("R1", "非法级别", "office-1", level=0)

    def test_review_records(self):
        review = self.service.create_review(
            "R1", "签证风险复盘", "office-1",
            root_cause="材料积压", actions=["增设预审", "双通道备份"],
        )
        self.assertEqual(review["actions"], ["增设预审", "双通道备份"])
        reviews = self.service.list_reviews(risk_key="R1")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["root_cause"], "材料积压")
        with self.assertRaises(NotFoundError):
            self.service.create_review("no-such-risk", "x", "office-1")

    def test_audit_trail_covers_operations(self):
        self.service.escalate("R1", "初判超时", "office-1")
        self.service.create_review("R1", "复盘", "office-1")
        actions = {a["action"] for a in self.service.list_audit()}
        self.assertIn("risk_opened", actions)
        self.assertIn("escalation_created", actions)
        self.assertIn("review_created", actions)


if __name__ == "__main__":
    unittest.main()
