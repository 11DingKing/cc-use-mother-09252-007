"""措施认领：并发互斥、租约到期、幂等重认。"""
from __future__ import annotations

import threading
import unittest

import helpers
from service_09252_007.errors import ConflictError


class ClaimTests(helpers.ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.add_project("A")
        self.raise_risk("R1", "A")
        self.measure = self.service.register_measure("R1", "加急办理签证", "coord-1")
        self.measure_id = self.measure["measure_id"]

    def test_concurrent_claim_single_winner(self):
        barrier = threading.Barrier(4)
        results = []
        lock = threading.Lock()

        def worker(name):
            barrier.wait()
            try:
                outcome = self.service.claim_measure(self.measure_id, owner=name, ttl_seconds=600)
                with lock:
                    results.append((name, "ok", outcome["lease"]["lease_id"]))
            except ConflictError:
                with lock:
                    results.append((name, "conflict", None))

        threads = [threading.Thread(target=worker, args=(f"owner-{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        barrier.wait()  # 同时放行，最大化竞争
        for t in threads:
            t.join()

        winners = [r for r in results if r[1] == "ok"]
        self.assertEqual(len(winners), 1, f"并发认领应只有一个赢家: {results}")
        self.assertEqual(len([r for r in results if r[1] == "conflict"]), 2)

        active = self.service.list_leases(status="active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["lease_id"], winners[0][2])
        measure = self.service.get_measure(self.measure_id)
        self.assertEqual(measure["status"], "claimed")
        self.assertEqual(measure["assignee"], winners[0][0])

    def test_same_owner_reclaim_is_idempotent(self):
        first = self.service.claim_measure(self.measure_id, "owner-1", ttl_seconds=600)
        again = self.service.claim_measure(self.measure_id, "owner-1", ttl_seconds=600)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(again["deduplicated"])
        self.assertEqual(first["lease"]["lease_id"], again["lease"]["lease_id"])
        self.assertEqual(len(self.service.list_leases(status="active")), 1)

    def test_lease_expiry_allows_reclaim(self):
        self.service.claim_measure(self.measure_id, "owner-1", ttl_seconds=60)
        self.clock.advance(seconds=120)
        outcome = self.service.claim_measure(self.measure_id, "owner-2", ttl_seconds=600)
        self.assertFalse(outcome["deduplicated"])
        leases = {l["owner"]: l["status"] for l in self.service.list_leases()}
        self.assertEqual(leases["owner-1"], "expired")
        self.assertEqual(leases["owner-2"], "active")
        self.assertEqual(self.service.get_measure(self.measure_id)["assignee"], "owner-2")

    def test_release_and_complete(self):
        self.service.claim_measure(self.measure_id, "owner-1", ttl_seconds=600)
        with self.assertRaises(ConflictError):
            self.service.complete_measure(self.measure_id, "owner-2")  # 非租约持有人
        self.service.complete_measure(self.measure_id, "owner-1")
        self.assertEqual(self.service.get_measure(self.measure_id)["status"], "done")
        with self.assertRaises(ConflictError):
            self.service.claim_measure(self.measure_id, "owner-3")  # 已完成不可再认领

    def test_release_returns_to_proposed(self):
        self.service.claim_measure(self.measure_id, "owner-1", ttl_seconds=600)
        self.service.release_measure(self.measure_id, "owner-1")
        measure = self.service.get_measure(self.measure_id)
        self.assertEqual(measure["status"], "proposed")
        self.assertIsNone(measure["assignee"])
        outcome = self.service.claim_measure(self.measure_id, "owner-2", ttl_seconds=600)
        self.assertFalse(outcome["deduplicated"])


if __name__ == "__main__":
    unittest.main()
