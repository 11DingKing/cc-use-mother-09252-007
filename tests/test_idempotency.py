"""乱序与重复消息的一致性：重复导入、撤销乱序、并发去重。"""
import unittest
from concurrent.futures import ThreadPoolExecutor

import helpers


class IdempotencyTests(helpers.ServiceTestCase):
    def test_duplicate_import_is_noop(self):
        helpers.build_graph(self.svc)
        first = self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])[0]
        self.assertEqual(first["status"], "imported")
        before = self.svc.dump_state()
        second = self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])[0]
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.svc.dump_state(), before)

    def test_same_event_id_with_different_payload_conflicts(self):
        helpers.build_graph(self.svc)
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])
        bad = helpers.make_event("E1", "A", "visa", "low")  # 同 id 不同级别
        result = self.svc.import_events([bad])[0]
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "event_payload_mismatch")

    def test_revoke_before_import_matches_import_before_revoke(self):
        def scenario(order):
            svc = self.fresh_service()
            helpers.build_graph(svc)
            if order == "import_first":
                svc.import_events([helpers.make_event("E1", "A", "visa", "critical")])
                svc.revoke_event("E1", "签证问题排除")
            else:
                svc.revoke_event("E1", "签证问题排除")
                svc.import_events([helpers.make_event("E1", "A", "visa", "critical")])
            return svc.dump_state()

        self.assertEqual(scenario("import_first"), scenario("revoke_first"))
        # 两种顺序下最终都无任何打开的风险
        svc = self.fresh_service()
        helpers.build_graph(svc)
        svc.revoke_event("E1", "签证问题排除")
        svc.import_events([helpers.make_event("E1", "A", "visa", "critical")])
        self.assertEqual(svc.impact_summary()["totals"]["open"], 0)

    def test_shuffled_and_duplicated_imports_converge(self):
        events = [
            helpers.make_event("E1", "A", "visa", "critical"),
            helpers.make_event("E2", "B", "visa", "high"),
            helpers.make_event("E3", "C", "visa", "medium"),
        ]
        orders = [
            events,
            list(reversed(events)),
            [events[1], events[0], events[1], events[2], events[0]],  # 乱序 + 重复
        ]
        states = []
        for order in orders:
            svc = self.fresh_service()
            helpers.build_graph(svc)
            svc.import_events(order)
            states.append(svc.dump_state())
        self.assertEqual(states[0], states[1])
        self.assertEqual(states[1], states[2])

    def test_concurrent_duplicate_import_yields_single_event(self):
        helpers.build_graph(self.svc)
        event = helpers.make_event("E1", "A", "visa", "high")

        def import_it(_):
            svc = self.new_service()
            return svc.import_events([event])[0]["status"]

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(import_it, range(6)))
        self.assertEqual(sorted(statuses), ["duplicate"] * 5 + ["imported"])
        self.assertEqual(len(self.svc.dump_state()["events"]), 1)

    def test_duplicate_revoke_is_idempotent(self):
        helpers.build_graph(self.svc)
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "high")])
        first = self.svc.revoke_event("E1", "撤销")
        second = self.svc.revoke_event("E1", "撤销")
        self.assertEqual(first["status"], "revoked")
        self.assertEqual(second["status"], "already_revoked")
        self.assertEqual(len(self.svc.dump_state()["events"]), 1)

    def test_duplicate_escalation_is_idempotent(self):
        helpers.build_graph(self.svc)
        self.svc.import_events([helpers.make_event("E1", "A", "visa", "medium")])
        self.svc.escalate("B:E1", esc_id="ESC-1", actor="李主任", to_severity="high")
        again = self.svc.escalate("B:E1", esc_id="ESC-1", actor="李主任",
                                  to_severity="high")
        self.assertEqual(again["severity"], "high")
        self.assertEqual(len(self.svc.dump_state()["escalations"]), 1)


if __name__ == "__main__":
    unittest.main()
