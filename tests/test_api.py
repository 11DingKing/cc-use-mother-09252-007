"""HTTP 接口冒烟测试：真实起服务，走完整业务链路。"""
import http.client
import json
import threading
import unittest

from service_09252_007.api.http import create_server

import helpers


class ApiTests(helpers.ServiceTestCase):
    @classmethod
    def setUpClass(cls):
        cls._clock = helpers.FakeClock()

    def setUp(self):
        super().setUp()
        self.server = create_server(self.svc, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_full_business_flow(self):
        # 登记项目与依赖
        status, _ = self._call("POST", "/projects", {
            "project_id": "A", "name": "阿尔法项目", "region": "CN"})
        self.assertEqual(status, 201)
        status, _ = self._call("POST", "/projects", {
            "project_id": "B", "name": "贝塔项目", "region": "GULF"})
        self.assertEqual(status, 201)
        status, _ = self._call("POST", "/dependencies", {
            "src": "A", "dst": "B", "dep_type": "visa"})
        self.assertEqual(status, 201)

        # 事件导入（含重复）
        status, data = self._call("POST", "/events/import", {"events": [
            {"event_id": "E1", "project_id": "A", "category": "visa",
             "severity": "critical", "occurred_at": "2026-09-25T08:00:00+00:00"},
            {"event_id": "E1", "project_id": "A", "category": "visa",
             "severity": "critical", "occurred_at": "2026-09-25T08:00:00+00:00"},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual([r["status"] for r in data["results"]],
                         ["imported", "duplicate"])

        # 影响查询：事件视角与项目视角
        status, data = self._call("GET", "/events/E1/impact")
        self.assertEqual(status, 200)
        self.assertEqual(data["impacts"][0]["project_id"], "B")
        self.assertEqual(data["impacts"][0]["deadline"], "2026-10-05")
        status, data = self._call("GET", "/projects/B/impact")
        self.assertEqual(status, 200)
        self.assertEqual(data["risks"][0]["risk_id"], "D:visa:B")

        # 措施认领与完成
        status, _ = self._call("POST", "/measures", {
            "measure_id": "M1", "risk_id": "D:visa:B", "title": "启动备用签证通道"})
        self.assertEqual(status, 201)
        status, data = self._call("POST", "/measures/M1/claim", {
            "owner": "赵老师", "token": "T1", "ttl_seconds": 600})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "claimed")
        status, data = self._call("POST", "/measures/M1/claim", {
            "owner": "钱老师", "token": "T2", "ttl_seconds": 600})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "lease_held")
        status, data = self._call("POST", "/measures/M1/complete", {"token": "T1"})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "done")

        # 升级与复盘
        status, data = self._call("POST", "/risks/D:visa:B/escalate", {
            "esc_id": "ESC-1", "actor": "李主任", "to_severity": "critical"})
        self.assertEqual(status, 200)
        self.assertEqual(data["severity"], "critical")
        status, _ = self._call("POST", "/risks/D:visa:B/resolve", {
            "actor": "李主任", "note": "已处置"})
        self.assertEqual(status, 200)
        status, _ = self._call("POST", "/risks/D:visa:B/postmortems", {
            "pm_id": "PM1", "root_cause": "签证政策突变", "created_by": "李主任"})
        self.assertEqual(status, 201)
        status, data = self._call("GET", "/risks/D:visa:B/postmortem")
        self.assertEqual(status, 200)
        self.assertEqual(data["root_cause"], "签证政策突变")

        # 豁免与汇总
        status, _ = self._call("POST", "/exemptions", {
            "exemption_id": "EX1", "confirmed_by": "王主任", "reason": "批次合并",
            "valid_from": "2026-09-25T00:00:00+00:00",
            "valid_until": "2026-10-02T00:00:00+00:00",
            "project_id": "B", "category": "visa"})
        self.assertEqual(status, 201)
        status, data = self._call("GET", "/impact/summary")
        self.assertEqual(status, 200)
        self.assertIn("B", data["projects"])

        # 维护与审计
        status, _ = self._call("POST", "/maintenance/sweep")
        self.assertEqual(status, 200)
        status, data = self._call("GET", "/audit?entity=risk&entity_id=D:visa:B")
        self.assertEqual(status, 200)
        actions = [e["action"] for e in data["entries"]]
        self.assertIn("risk_escalated", actions)

    def test_error_shape_and_unknown_route(self):
        status, data = self._call("GET", "/no/such/route")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "not_found")
        status, data = self._call("POST", "/projects", {"project_id": "X"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)
        status, data = self._call("GET", "/risks/B:missing")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "risk_not_found")

    def test_invalid_json_body(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/projects", body=b"{not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertEqual(data["error"]["code"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
