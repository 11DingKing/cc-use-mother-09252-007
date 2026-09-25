"""人工确认豁免：明确有效期、到期失效、作用域与撤销。"""
import unittest

from service_09252_007.errors import Conflict, InvalidInput

import helpers


class ExemptionTests(helpers.ServiceTestCase):
    def setUp(self):
        super().setUp()
        helpers.build_graph(self.svc)
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])

    def _exempt(self, **kwargs):
        params = dict(exemption_id="EX1", confirmed_by="国际处王主任",
                      reason="该批次学生已改用团体签证通道",
                      valid_from="2026-09-25T00:00:00+00:00",
                      valid_until="2026-10-02T00:00:00+00:00",
                      risk_id="D:visa:B")
        params.update(kwargs)
        return self.svc.create_exemption(**params)

    def test_valid_exemption_marks_risk_exempted(self):
        self._exempt()
        impact = self.svc.project_impact("B")
        derived = next(r for r in impact["risks"] if r["risk_id"] == "D:visa:B")
        self.assertEqual(derived["status"], "open")
        self.assertEqual(derived["effective_status"], "exempted")
        summary = self.svc.impact_summary()
        self.assertEqual(summary["projects"]["B"]["exempted"], 1)
        self.assertEqual(summary["projects"]["B"]["open"], 0)

    def test_expired_exemption_no_longer_applies(self):
        self._exempt()
        self.clock.advance(days=8)  # 超过 valid_until
        derived = self.svc.get_risk("D:visa:B")
        self.assertEqual(derived["effective_status"], "open")

    def test_not_yet_valid_exemption_does_not_apply(self):
        self._exempt(valid_from="2026-09-26T00:00:00+00:00",
                     valid_until="2026-10-02T00:00:00+00:00")
        self.assertEqual(self.svc.get_risk("D:visa:B")["effective_status"], "open")

    def test_revoked_exemption_no_longer_applies(self):
        self._exempt()
        self.svc.revoke_exemption("EX1", actor="纪检")
        self.assertEqual(self.svc.get_risk("D:visa:B")["effective_status"], "open")

    def test_project_category_scope_covers_derived_risks(self):
        self._exempt(exemption_id="EX2", risk_id=None, project_id="B", category="visa")
        self.assertEqual(self.svc.get_risk("D:visa:B")["effective_status"], "exempted")

    def test_confirmation_and_validity_window_required(self):
        with self.assertRaises(InvalidInput):
            self._exempt(exemption_id="EX3", confirmed_by=None)
        with self.assertRaises(InvalidInput):
            self._exempt(exemption_id="EX4", valid_from="2026-10-02T00:00:00+00:00",
                         valid_until="2026-09-25T00:00:00+00:00")
        with self.assertRaises(InvalidInput):
            # 既不指定 risk_id 也不指定 项目+类别
            self._exempt(exemption_id="EX5", risk_id=None)

    def test_exemption_id_idempotent_and_conflict_on_different_payload(self):
        first = self._exempt()
        again = self._exempt()
        self.assertEqual(first["exemption_id"], again["exemption_id"])
        with self.assertRaises(Conflict):
            self._exempt(reason="另一个理由")

    def test_exemption_survives_restart(self):
        self._exempt()
        svc2 = self.new_service()
        self.assertEqual(svc2.get_risk("D:visa:B")["effective_status"], "exempted")


if __name__ == "__main__":
    unittest.main()
