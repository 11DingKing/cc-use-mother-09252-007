"""HTTP 边界：端到端流程与错误映射。"""
from __future__ import annotations

import http.client
import json
import threading
import unittest

import helpers
from service_09252_007.api import create_server


class ApiTests(helpers.ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.server = create_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def test_full_workflow(self):
        # 登记项目与依赖
        status, _ = self.call("POST", "/projects", {
            "project_id": "A", "name": "中英项目", "region": "CN", "owner": "office-1",
        })
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/projects", {
            "project_id": "B", "name": "中澳项目", "region": "UK",
        })
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/dependencies", {
            "dependency_id": "D1", "upstream": "A", "downstream": "B", "kind": "shared-faculty",
        })
        self.assertEqual(status, 201)

        # 事件导入（含重复消息）
        event = {
            "message_id": "m-1",
            "event_type": "risk_raised",
            "occurred_at": "2026-09-25T09:00:00+00:00",
            "payload": {"risk_id": "R1", "project_id": "A", "category": "visa", "severity": 5},
        }
        status, data = self.call("POST", "/events/import", {"events": [event]})
        self.assertEqual((status, data["imported"]), (200, 1))
        status, data = self.call("POST", "/events/import", {"events": [event]})
        self.assertEqual((data["imported"], data["duplicates"]), (0, 1))

        # 影响查询：下游项目看到派生风险，基础风险看到传播范围
        status, impact = self.call("GET", "/projects/B/impact")
        self.assertEqual(status, 200)
        self.assertEqual(impact["risks"][0]["risk_key"], "derived:B:visa")
        self.assertEqual(impact["risks"][0]["sources"][0]["base_risk_id"], "R1")
        status, prop = self.call("GET", "/risks/R1/propagation")
        self.assertEqual([a["project_id"] for a in prop["affected_projects"]], ["B"])

        # 措施认领：第二人认领冲突
        status, measure = self.call("POST", "/measures", {
            "risk_key": "derived:B:visa", "title": "替换师资方案", "created_by": "coord-1",
        })
        self.assertEqual(status, 201)
        mid = measure["measure_id"]
        status, claim = self.call("POST", f"/measures/{mid}/claim",
                                  {"owner": "team-uk", "ttl_seconds": 600})
        self.assertEqual(status, 200)
        self.assertFalse(claim["deduplicated"])
        status, conflict = self.call("POST", f"/measures/{mid}/claim", {"owner": "team-cn"})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "conflict")

        # 豁免
        status, ex = self.call("POST", "/exemptions", {
            "risk_key": "derived:B:visa", "approved_by": "director-1",
            "reason": "批次顺延已确认",
            "valid_from": "2026-09-25T00:00:00+00:00",
            "valid_until": "2026-10-25T00:00:00+00:00",
        })
        self.assertEqual(status, 201)
        status, risk = self.call("GET", "/risks/derived:B:visa")
        self.assertEqual(risk["effective_status"], "exempt")

        # 升级
        status, esc = self.call("POST", "/risks/derived:B:visa/escalations",
                                {"reason": "临近截止", "created_by": "office-1"})
        self.assertEqual((status, esc["level"]), (201, 1))

        # 复盘
        status, review = self.call("POST", "/reviews", {
            "risk_key": "derived:B:visa", "summary": "签证延误联动复盘",
            "root_cause": "单一签证通道", "actions": ["建立备用通道"],
            "created_by": "office-1",
        })
        self.assertEqual(status, 201)
        status, reviews = self.call("GET", "/reviews?risk_key=derived:B:visa")
        self.assertEqual(len(reviews["reviews"]), 1)

        # 审计
        status, audit = self.call("GET", "/audit?entity_type=risk&entity_id=derived:B:visa")
        self.assertTrue(any(a["action"] == "risk_opened" for a in audit["audit"]))

    def test_error_mapping(self):
        status, data = self.call("POST", "/projects", {"project_id": "A"})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "validation_error")

        status, data = self.call("GET", "/risks/nope")
        self.assertEqual(status, 404)

        status, data = self.call("GET", "/no-such-route")
        self.assertEqual(status, 404)

        status, data = self.call("POST", "/events/import", {
            "events": [{"message_id": "x", "event_type": "bogus", "payload": {}}],
        })
        self.assertEqual(status, 400)

        status, data = self.call("POST", "/measures/whatever/claim", {"owner": "x"})
        self.assertEqual(status, 404)

    def test_health(self):
        status, data = self.call("GET", "/health")
        self.assertEqual((status, data["status"]), (200, "ok"))


if __name__ == "__main__":
    unittest.main()
