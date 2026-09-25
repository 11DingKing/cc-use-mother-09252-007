"""重启恢复：租约与审计在 SQLite 中持久化，重启后可恢复。"""
from __future__ import annotations

import unittest

import helpers


class RecoveryTests(helpers.ServiceTestCase):
    def test_leases_and_audit_survive_restart(self):
        self.add_project("A")
        self.raise_risk("R1", "A")
        measure = self.service.register_measure("R1", "加急办理签证", "coord-1")
        self.service.claim_measure(measure["measure_id"], "owner-1", ttl_seconds=3600)
        audit_before = self.service.list_audit()
        self.assertTrue(any(a["action"] == "lease_acquired" for a in audit_before))

        # 模拟重启：关闭后重新打开同一数据库
        self.service.close()
        restarted = self.make_service()
        self.addCleanup(restarted.close)

        leases = restarted.list_leases(status="active")
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0]["owner"], "owner-1")
        self.assertEqual(restarted.get_measure(measure["measure_id"])["status"], "claimed")

        audit_after = restarted.list_audit()
        self.assertEqual(
            [a["audit_id"] for a in audit_before],
            [a["audit_id"] for a in audit_after],
        )
        self.assertEqual(restarted.get_risk("R1")["status"], "open")

    def test_expired_lease_swept_on_startup(self):
        self.add_project("A")
        self.raise_risk("R1", "A")
        measure = self.service.register_measure("R1", "加急办理签证", "coord-1")
        self.service.claim_measure(measure["measure_id"], "owner-1", ttl_seconds=60)
        self.service.close()

        self.clock.advance(seconds=3600)  # 重启时租约已过期
        restarted = self.make_service()
        self.addCleanup(restarted.close)

        self.assertEqual(restarted.list_leases(status="active"), [])
        self.assertEqual(restarted.list_leases(status="expired")[0]["owner"], "owner-1")
        self.assertEqual(restarted.get_measure(measure["measure_id"])["status"], "proposed")
        actions = [a["action"] for a in restarted.list_audit()]
        self.assertIn("lease_expired", actions)

        # 过期释放后可以被他人认领
        outcome = restarted.claim_measure(measure["measure_id"], "owner-2", ttl_seconds=600)
        self.assertFalse(outcome["deduplicated"])


if __name__ == "__main__":
    unittest.main()
