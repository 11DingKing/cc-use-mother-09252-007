"""措施认领：并发认领唯一胜者、租约到期与重启恢复。"""
import unittest
from concurrent.futures import ThreadPoolExecutor

from service_09252_007.errors import Conflict

import helpers


class ClaimTests(helpers.ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.svc.create_project("A", "阿尔法项目", "CN")
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])
        self.svc.create_measure("M1", "B:E1", "补办签证材料", created_by="王秘书")

    def _try_claim(self, idx):
        svc = self.new_service()
        try:
            svc.claim_measure("M1", owner=f"owner-{idx}", token=f"token-{idx}",
                              ttl_seconds=600)
            return "claimed"
        except Conflict as exc:
            return exc.code

    def test_concurrent_claims_have_exactly_one_winner(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self._try_claim, range(8)))
        self.assertEqual(results.count("claimed"), 1)
        self.assertEqual(results.count("lease_held"), 7)
        leases = self.svc.dump_state()["leases"]
        self.assertEqual(len(leases), 1)

    def test_concurrent_claims_on_distinct_measures_all_succeed(self):
        for i in range(4):
            self.svc.create_measure(f"MX{i}", "B:E1", f"措施{i}")

        def claim(idx):
            svc = self.new_service()
            svc.claim_measure(f"MX{idx}", owner=f"o{idx}", token=f"t{idx}",
                              ttl_seconds=600)
            return idx

        with ThreadPoolExecutor(max_workers=4) as pool:
            done = list(pool.map(claim, range(4)))
        self.assertEqual(sorted(done), [0, 1, 2, 3])
        self.assertEqual(len(self.svc.dump_state()["leases"]), 4)

    def test_claim_retry_with_same_token_is_idempotent(self):
        first = self.svc.claim_measure("M1", owner="赵老师", token="T1", ttl_seconds=600)
        second = self.svc.claim_measure("M1", owner="赵老师", token="T1", ttl_seconds=600)
        self.assertEqual(first["status"], "claimed")
        self.assertEqual(second["status"], "already_claimed")
        self.assertEqual(first["lease"]["expires_at"], second["lease"]["expires_at"])
        self.assertEqual(len(self.svc.dump_state()["leases"]), 1)

    def test_lease_expiry_allows_reclaim(self):
        self.svc.claim_measure("M1", owner="赵老师", token="T1", ttl_seconds=30)
        self.clock.advance(seconds=31)
        again = self.svc.claim_measure("M1", owner="钱老师", token="T2", ttl_seconds=600)
        self.assertEqual(again["status"], "claimed")
        self.assertEqual(again["lease"]["owner"], "钱老师")

    def test_restart_preserves_lease_and_audit(self):
        self.svc.claim_measure("M1", owner="赵老师", token="T1", ttl_seconds=3600)
        # 模拟重启：同一数据库文件重建服务
        svc2 = self.new_service()
        with self.assertRaises(Conflict) as ctx:
            svc2.claim_measure("M1", owner="钱老师", token="T2", ttl_seconds=600)
        self.assertEqual(ctx.exception.code, "lease_held")
        self.assertEqual(ctx.exception.details["owner"], "赵老师")
        # 审计在重启后仍可查
        actions = [e["action"] for e in svc2.audit_log(entity="measure", entity_id="M1")]
        self.assertIn("measure_claimed", actions)
        # 租约到期后（再次重启）可认领
        self.clock.advance(seconds=3601)
        svc3 = self.new_service()
        result = svc3.claim_measure("M1", owner="钱老师", token="T2", ttl_seconds=600)
        self.assertEqual(result["status"], "claimed")

    def test_complete_requires_valid_token(self):
        self.svc.claim_measure("M1", owner="赵老师", token="T1", ttl_seconds=600)
        with self.assertRaises(Conflict) as ctx:
            self.svc.complete_measure("M1", token="WRONG")
        self.assertEqual(ctx.exception.code, "token_mismatch")
        done = self.svc.complete_measure("M1", token="T1", actor="赵老师")
        self.assertEqual(done["status"], "done")
        self.assertIsNone(done["lease"])
        # 完成后不可再认领
        with self.assertRaises(Conflict) as ctx:
            self.svc.claim_measure("M1", owner="钱老师", token="T2")
        self.assertEqual(ctx.exception.code, "measure_not_open")

    def test_complete_without_claim_or_after_expiry_fails(self):
        with self.assertRaises(Conflict) as ctx:
            self.svc.complete_measure("M1", token="T1")
        self.assertEqual(ctx.exception.code, "not_claimed")
        self.svc.claim_measure("M1", owner="赵老师", token="T1", ttl_seconds=10)
        self.clock.advance(seconds=11)
        with self.assertRaises(Conflict) as ctx:
            self.svc.complete_measure("M1", token="T1")
        self.assertEqual(ctx.exception.code, "lease_expired")

    def test_heartbeat_extends_lease(self):
        claimed = self.svc.claim_measure("M1", owner="赵老师", token="T1", ttl_seconds=60)
        self.clock.advance(seconds=50)
        renewed = self.svc.heartbeat("M1", token="T1", ttl_seconds=60)
        self.assertGreater(renewed["expires_at"], claimed["lease"]["expires_at"])
        self.clock.advance(seconds=55)  # 原租约已过期，续租仍有效
        with self.assertRaises(Conflict):
            self.svc.claim_measure("M1", owner="钱老师", token="T2")

    def test_release_frees_measure(self):
        self.svc.claim_measure("M1", owner="赵老师", token="T1", ttl_seconds=600)
        self.svc.release_measure("M1", token="T1")
        result = self.svc.claim_measure("M1", owner="钱老师", token="T2", ttl_seconds=600)
        self.assertEqual(result["status"], "claimed")

    def test_sweep_expires_leases(self):
        self.svc.claim_measure("M1", owner="赵老师", token="T1", ttl_seconds=30)
        self.clock.advance(seconds=31)
        result = self.svc.sweep()
        self.assertEqual(len(result["expired_leases"]), 1)
        self.assertEqual(self.svc.dump_state()["leases"], [])


if __name__ == "__main__":
    unittest.main()
