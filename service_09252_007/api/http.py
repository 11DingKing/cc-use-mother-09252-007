"""HTTP 接口边界：基于标准库的 JSON API。

仅做协议适配，业务规则全部在应用服务层。错误统一为
{"error": {"code", "message", "details"}}，HTTP 状态由领域错误类型决定。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..errors import DomainError

ROUTES = []


def route(method, pattern):
    def decorator(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return decorator


@route("POST", r"/projects")
def _create_project(svc, body, query):
    return 201, svc.create_project(body.get("project_id"), body.get("name"),
                                   body.get("region"), actor=body.get("actor"))


@route("POST", r"/calendars")
def _register_calendar(svc, body, query):
    return 200, svc.register_calendar(body.get("region"), body.get("weekend"),
                                      body.get("holidays") or [], actor=body.get("actor"))


@route("POST", r"/dependencies")
def _add_dependency(svc, body, query):
    return 201, svc.add_dependency(body.get("src"), body.get("dst"), body.get("dep_type"),
                                   dep_id=body.get("dep_id"), actor=body.get("actor"))


@route("POST", r"/rules")
def _register_rules(svc, body, query):
    return 201, svc.register_rules(body.get("version"), body.get("rules"),
                                   activate=body.get("activate", True),
                                   actor=body.get("actor"))


@route("GET", r"/rules/active")
def _active_rules(svc, body, query):
    return 200, svc.active_rules()


@route("POST", r"/events/import")
def _import_events(svc, body, query):
    return 200, {"results": svc.import_events(body.get("events"),
                                              actor=body.get("actor"))}


@route("POST", r"/events/(?P<event_id>[^/]+)/revoke")
def _revoke_event(svc, body, query, event_id):
    return 200, svc.revoke_event(event_id, reason=body.get("reason"),
                                 actor=body.get("actor"),
                                 revoked_at=body.get("revoked_at"))


@route("GET", r"/events/(?P<event_id>[^/]+)/impact")
def _event_impact(svc, body, query, event_id):
    return 200, svc.event_impact(event_id)


@route("GET", r"/projects/(?P<project_id>[^/]+)/impact")
def _project_impact(svc, body, query, project_id):
    return 200, svc.project_impact(project_id)


@route("GET", r"/impact/summary")
def _impact_summary(svc, body, query):
    return 200, svc.impact_summary()


@route("GET", r"/risks/(?P<risk_id>[^/]+)")
def _get_risk(svc, body, query, risk_id):
    return 200, svc.get_risk(risk_id)


@route("POST", r"/risks/(?P<risk_id>[^/]+)/resolve")
def _resolve_risk(svc, body, query, risk_id):
    return 200, svc.resolve_risk(risk_id, actor=body.get("actor"), note=body.get("note"))


@route("POST", r"/risks/(?P<risk_id>[^/]+)/escalate")
def _escalate(svc, body, query, risk_id):
    return 200, svc.escalate(risk_id, esc_id=body.get("esc_id"), actor=body.get("actor"),
                             reason=body.get("reason"),
                             to_severity=body.get("to_severity"))


@route("POST", r"/risks/(?P<risk_id>[^/]+)/postmortems")
def _create_postmortem(svc, body, query, risk_id):
    return 201, svc.create_postmortem(
        risk_id, pm_id=body.get("pm_id"), root_cause=body.get("root_cause"),
        timeline=body.get("timeline"), lessons=body.get("lessons"),
        created_by=body.get("created_by"))


@route("GET", r"/risks/(?P<risk_id>[^/]+)/postmortem")
def _get_postmortem(svc, body, query, risk_id):
    return 200, svc.get_postmortem(risk_id)


@route("POST", r"/measures")
def _create_measure(svc, body, query):
    return 201, svc.create_measure(body.get("measure_id"), body.get("risk_id"),
                                   body.get("title"), created_by=body.get("created_by"))


@route("GET", r"/measures/(?P<measure_id>[^/]+)")
def _get_measure(svc, body, query, measure_id):
    return 200, svc.get_measure(measure_id)


@route("POST", r"/measures/(?P<measure_id>[^/]+)/claim")
def _claim(svc, body, query, measure_id):
    return 200, svc.claim_measure(measure_id, owner=body.get("owner"),
                                  token=body.get("token"),
                                  ttl_seconds=body.get("ttl_seconds", 3600))


@route("POST", r"/measures/(?P<measure_id>[^/]+)/heartbeat")
def _heartbeat(svc, body, query, measure_id):
    return 200, svc.heartbeat(measure_id, token=body.get("token"),
                              ttl_seconds=body.get("ttl_seconds", 3600))


@route("POST", r"/measures/(?P<measure_id>[^/]+)/release")
def _release(svc, body, query, measure_id):
    return 200, svc.release_measure(measure_id, token=body.get("token"))


@route("POST", r"/measures/(?P<measure_id>[^/]+)/complete")
def _complete(svc, body, query, measure_id):
    return 200, svc.complete_measure(measure_id, token=body.get("token"),
                                     actor=body.get("actor"))


@route("POST", r"/exemptions")
def _create_exemption(svc, body, query):
    return 201, svc.create_exemption(
        body.get("exemption_id"), confirmed_by=body.get("confirmed_by"),
        reason=body.get("reason"), valid_from=body.get("valid_from"),
        valid_until=body.get("valid_until"), risk_id=body.get("risk_id"),
        project_id=body.get("project_id"), category=body.get("category"),
        actor=body.get("actor"))


@route("POST", r"/exemptions/(?P<exemption_id>[^/]+)/revoke")
def _revoke_exemption(svc, body, query, exemption_id):
    return 200, svc.revoke_exemption(exemption_id, actor=body.get("actor"))


@route("POST", r"/maintenance/sweep")
def _sweep(svc, body, query):
    return 200, svc.sweep()


@route("GET", r"/audit")
def _audit(svc, body, query):
    return 200, {"entries": svc.audit_log(entity=query.get("entity"),
                                          entity_id=query.get("entity_id"))}


class _Handler(BaseHTTPRequestHandler):
    server_version = "RiskLinkage/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, *args):  # 静默访问日志
        pass

    def _dispatch(self, method):
        try:
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if raw:
                    body = json.loads(raw.decode("utf-8"))
                    if not isinstance(body, dict):
                        raise DomainError("invalid_body", "请求体必须是 JSON 对象")
            for m, rx, fn in ROUTES:
                if m != method:
                    continue
                match = rx.match(parsed.path)
                if match:
                    status, payload = fn(self.server.risk_service, body, query,
                                         **match.groupdict())
                    return self._send(status, payload)
            self._send(404, {"error": {"code": "not_found", "message": "未知路由",
                                       "details": {}}})
        except DomainError as exc:
            self._send(exc.http_status, {"error": exc.to_dict()})
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": {"code": "invalid_json",
                                       "message": "请求体不是合法 JSON", "details": {}}})
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"error": {"code": "internal", "message": str(exc),
                                       "details": {}}})

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def create_server(service, host="127.0.0.1", port=8080):
    """构建 HTTP 服务（调用方负责 serve_forever / shutdown）。"""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.risk_service = service
    return server
