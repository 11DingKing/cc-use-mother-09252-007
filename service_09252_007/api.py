"""HTTP 接口边界：基于标准库的 JSON API，仅做参数搬运与错误映射。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .errors import RiskServiceError, ValidationError
from .timeutil import fmt

_ROUTES: list[tuple[str, re.Pattern, object]] = []


def _route(method: str, pattern: str):
    regex = re.compile(pattern)

    def decorator(handler):
        _ROUTES.append((method, regex, handler))
        return handler

    return decorator


def _require(body: dict, *fields: str) -> None:
    missing = [f for f in fields if body.get(f) in (None, "")]
    if missing:
        raise ValidationError(f"缺少必填字段: {', '.join(missing)}")


def _actor(body: dict) -> str:
    return body.get("actor") or "api"


# ----------------------------------------------------------------------
# 基础与事件导入
# ----------------------------------------------------------------------
@_route("GET", r"/health")
def _health(service, body, query):
    return {"status": "ok", "time": fmt(service.clock())}


@_route("POST", r"/events/import")
def _import_events(service, body, query):
    _require(body, "events")
    return service.import_events(body["events"], actor=body.get("actor") or "system")


# ----------------------------------------------------------------------
# 项目、依赖、规则、节假日登记
# ----------------------------------------------------------------------
@_route("POST", r"/projects")
def _create_project(service, body, query):
    _require(body, "project_id", "name", "region")
    message_id = service.register_project(
        body["project_id"], body["name"], body["region"],
        owner=body.get("owner", ""), actor=_actor(body), occurred_at=body.get("occurred_at"),
    )
    return 201, {"message_id": message_id}


@_route("GET", r"/projects")
def _list_projects(service, body, query):
    return {"projects": service.list_projects()}


@_route("GET", r"/projects/(?P<project_id>[^/]+)/impact")
def _project_impact(service, body, query, project_id):
    return service.project_impact(project_id)


@_route("POST", r"/dependencies")
def _add_dependency(service, body, query):
    _require(body, "dependency_id", "upstream", "downstream", "kind")
    message_id = service.add_dependency(
        body["dependency_id"], body["upstream"], body["downstream"], body["kind"],
        actor=_actor(body), occurred_at=body.get("occurred_at"),
    )
    return 201, {"message_id": message_id}


@_route("GET", r"/dependencies")
def _list_dependencies(service, body, query):
    return {"dependencies": service.list_dependencies()}


@_route("DELETE", r"/dependencies/(?P<dependency_id>[^/]+)")
def _remove_dependency(service, body, query, dependency_id):
    message_id = service.remove_dependency(
        dependency_id, actor=_actor(body), occurred_at=body.get("occurred_at")
    )
    return {"message_id": message_id}


@_route("POST", r"/rules")
def _publish_rule(service, body, query):
    _require(body, "version", "effective_from")
    rule = {k: v for k, v in body.items() if k not in ("actor", "occurred_at")}
    message_id = service.publish_rule(rule, actor=_actor(body), occurred_at=body.get("occurred_at"))
    return 201, {"message_id": message_id}


@_route("GET", r"/rules")
def _list_rules(service, body, query):
    return {"rules": service.list_rules()}


@_route("POST", r"/holidays")
def _register_holiday(service, body, query):
    _require(body, "region", "day")
    message_id = service.register_holiday(
        body["region"], body["day"], actor=_actor(body), occurred_at=body.get("occurred_at")
    )
    return 201, {"message_id": message_id}


@_route("GET", r"/holidays")
def _list_holidays(service, body, query):
    return {"holidays": service.list_holidays(region=query.get("region"))}


# ----------------------------------------------------------------------
# 风险：发生、撤销、查询、影响
# ----------------------------------------------------------------------
@_route("POST", r"/risks/raise")
def _raise_risk(service, body, query):
    _require(body, "risk_id", "project_id", "category", "severity")
    message_id = service.raise_risk(
        body["risk_id"], body["project_id"], body["category"], body["severity"],
        reason=body.get("reason", ""), actor=_actor(body),
        occurred_at=body.get("occurred_at"),
    )
    return 201, {"message_id": message_id}


@_route("POST", r"/risks/revoke")
def _revoke_risk(service, body, query):
    _require(body, "risk_id")
    message_id = service.revoke_risk(
        body["risk_id"], reason=body.get("reason", ""), actor=_actor(body),
        occurred_at=body.get("occurred_at"),
    )
    return 201, {"message_id": message_id}


@_route("GET", r"/risks/overdue")
def _overdue_risks(service, body, query):
    return {"risks": service.overdue_risks()}


@_route("GET", r"/risks/(?P<risk_key>[^/]+)/propagation")
def _risk_propagation(service, body, query, risk_key):
    return service.risk_propagation(risk_key)


@_route("POST", r"/risks/(?P<risk_key>[^/]+)/escalations")
def _escalate(service, body, query, risk_key):
    _require(body, "reason", "created_by")
    return 201, service.escalate(
        risk_key, body["reason"], body["created_by"], level=body.get("level")
    )


@_route("GET", r"/risks/(?P<risk_key>[^/]+)/escalations")
def _list_escalations(service, body, query, risk_key):
    return {"escalations": service.list_escalations(risk_key=risk_key)}


@_route("GET", r"/risks/(?P<risk_key>[^/]+)")
def _get_risk(service, body, query, risk_key):
    return service.get_risk(risk_key)


@_route("GET", r"/risks")
def _list_risks(service, body, query):
    return {
        "risks": service.list_risks(
            project_id=query.get("project_id"),
            category=query.get("category"),
            kind=query.get("kind"),
            status=query.get("status"),
            effective_status=query.get("effective_status"),
            overdue=query.get("overdue") == "true",
        )
    }


# ----------------------------------------------------------------------
# 豁免
# ----------------------------------------------------------------------
@_route("POST", r"/exemptions")
def _grant_exemption(service, body, query):
    _require(body, "risk_key", "approved_by", "reason", "valid_from", "valid_until")
    return 201, service.grant_exemption(
        body["risk_key"], body["approved_by"], body["reason"],
        body["valid_from"], body["valid_until"],
    )


@_route("GET", r"/exemptions")
def _list_exemptions(service, body, query):
    return {
        "exemptions": service.list_exemptions(
            risk_key=query.get("risk_key"),
            active_only=query.get("active_only") == "true",
        )
    }


@_route("POST", r"/exemptions/(?P<exemption_id>[^/]+)/revoke")
def _revoke_exemption(service, body, query, exemption_id):
    return service.revoke_exemption(exemption_id, actor=_actor(body))


# ----------------------------------------------------------------------
# 缓解措施与认领
# ----------------------------------------------------------------------
@_route("POST", r"/measures")
def _register_measure(service, body, query):
    _require(body, "risk_key", "title", "created_by")
    return 201, service.register_measure(
        body["risk_key"], body["title"], body["created_by"],
        description=body.get("description", ""),
    )


@_route("GET", r"/measures")
def _list_measures(service, body, query):
    return {
        "measures": service.list_measures(
            risk_key=query.get("risk_key"), status=query.get("status")
        )
    }


@_route("POST", r"/measures/(?P<measure_id>[^/]+)/claim")
def _claim_measure(service, body, query, measure_id):
    _require(body, "owner")
    return service.claim_measure(
        measure_id, body["owner"], ttl_seconds=body.get("ttl_seconds", 3600)
    )


@_route("POST", r"/measures/(?P<measure_id>[^/]+)/release")
def _release_measure(service, body, query, measure_id):
    _require(body, "owner")
    return service.release_measure(measure_id, body["owner"])


@_route("POST", r"/measures/(?P<measure_id>[^/]+)/complete")
def _complete_measure(service, body, query, measure_id):
    _require(body, "owner")
    return service.complete_measure(measure_id, body["owner"])


@_route("GET", r"/leases")
def _list_leases(service, body, query):
    return {"leases": service.list_leases(status=query.get("status"))}


# ----------------------------------------------------------------------
# 复盘与审计
# ----------------------------------------------------------------------
@_route("POST", r"/reviews")
def _create_review(service, body, query):
    _require(body, "risk_key", "summary", "created_by")
    return 201, service.create_review(
        body["risk_key"], body["summary"], body["created_by"],
        root_cause=body.get("root_cause", ""), actions=body.get("actions"),
    )


@_route("GET", r"/reviews")
def _list_reviews(service, body, query):
    return {"reviews": service.list_reviews(risk_key=query.get("risk_key"))}


@_route("GET", r"/audit")
def _list_audit(service, body, query):
    try:
        limit = int(query.get("limit", 500))
    except (TypeError, ValueError):
        raise ValidationError("limit 必须是整数") from None
    return {
        "audit": service.list_audit(
            entity_type=query.get("entity_type"),
            entity_id=query.get("entity_id"),
            limit=limit,
        )
    }


# ----------------------------------------------------------------------
# 请求分发
# ----------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "RiskLinkage/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            body = self._read_body()
        except ValidationError as exc:
            return self._respond(exc.status, exc.payload())
        for route_method, regex, handler in _ROUTES:
            if route_method != method:
                continue
            match = regex.fullmatch(path)
            if not match:
                continue
            try:
                result = handler(self.server.risk_service, body, query, **match.groupdict())
                status, payload = result if isinstance(result, tuple) else (200, result)
            except RiskServiceError as exc:
                status, payload = exc.status, exc.payload()
            except Exception as exc:  # 兜底，避免连接悬挂
                status, payload = 500, {
                    "error": {"code": "internal_error", "message": str(exc), "details": {}}
                }
            return self._respond(status, payload)
        self._respond(
            404,
            {"error": {"code": "not_found", "message": f"无此路由: {method} {path}", "details": {}}},
        )

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValidationError("请求体不是合法 JSON") from None
        if not isinstance(body, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return body

    def _respond(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默访问日志
        pass


def create_server(service, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """创建 HTTP 服务；service 为 RiskService 实例，线程间共享。"""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.risk_service = service
    return server
