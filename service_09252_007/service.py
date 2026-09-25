"""应用服务：事件导入、影响查询、豁免、措施认领、升级与复盘。

所有改变风险状态的操作都归结为事件写入 + 投影重建，运行期状态
（措施、租约、豁免、升级、复盘）走独立表并同事务写审计。
时间通过 clock 注入，测试可用假时钟稳定复现。
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from pathlib import Path

from . import projection
from .errors import ConflictError, NotFoundError, ValidationError
from .rules import normalize_rule
from .storage import Database
from .timeutil import fmt, utcnow

EVENT_SCHEMAS = {
    "project_registered": ("project_id", "name", "region"),
    "dependency_added": ("dependency_id", "upstream", "downstream", "kind"),
    "dependency_removed": ("dependency_id",),
    "holiday_registered": ("region", "day"),
    "rule_published": ("version", "effective_from"),
    "risk_raised": ("risk_id", "project_id", "category", "severity"),
    "risk_revoked": ("risk_id",),
}

# 风险键以冒号拼接，相关标识符不允许出现冒号与斜杠
_ID_FIELDS = ("project_id", "category", "risk_id", "dependency_id", "upstream", "downstream", "region")

MAX_CLAIM_TTL_SECONDS = 30 * 24 * 3600


class RiskService:
    """合作办学风险联动应用服务。

    clock: 可替换时间源，返回带时区 datetime；缺省为系统 UTC 时间。
    """

    def __init__(self, db_path: str | Path, clock=None):
        self.db = Database(db_path)
        self.clock = clock or utcnow
        self._sweep_expired_leases()  # 重启恢复：清理已过期租约

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------
    # 事件导入
    # ------------------------------------------------------------------
    def import_events(self, events: list[dict], actor: str = "system") -> dict:
        """批量导入事件。message_id 幂等去重，乱序到达不影响最终状态。"""
        if not isinstance(events, list) or not events:
            raise ValidationError("events 必须是非空列表")
        normalized = []
        errors = []
        for index, raw in enumerate(events):
            try:
                normalized.append(self._normalize_event(raw))
            except ValidationError as exc:
                errors.append({"index": index, "message": exc.message})
        if errors:
            raise ValidationError("存在非法事件", {"errors": errors})

        received_at = fmt(self.clock())
        imported = 0
        duplicates = 0
        with self.db.tx() as conn:
            for event in normalized:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO events(message_id, event_type, occurred_at,"
                    " received_at, actor, payload) VALUES (?,?,?,?,?,?)",
                    (
                        event["message_id"],
                        event["event_type"],
                        event["occurred_at"],
                        received_at,
                        event.get("actor") or actor,
                        json.dumps(event["payload"], sort_keys=True, ensure_ascii=False),
                    ),
                )
                if cursor.rowcount:
                    imported += 1
                else:
                    duplicates += 1
            summary = projection.rebuild(conn) if imported else None
        result = {"imported": imported, "duplicates": duplicates}
        if summary is not None:
            result["transitions"] = {k: len(v) for k, v in summary.items()}
        return result

    def _normalize_event(self, raw) -> dict:
        if not isinstance(raw, dict):
            raise ValidationError("事件必须是 JSON 对象")
        message_id = raw.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValidationError("事件缺少 message_id")
        event_type = raw.get("event_type")
        if event_type not in EVENT_SCHEMAS:
            raise ValidationError(f"未知事件类型: {event_type!r}")
        occurred_raw = raw.get("occurred_at")
        if occurred_raw is None:
            occurred_at = fmt(self.clock())
        else:
            try:
                occurred_at = fmt(occurred_raw)
            except (ValueError, TypeError) as exc:
                raise ValidationError(f"occurred_at 非法: {exc}") from exc
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise ValidationError("事件缺少 payload 对象")
        for field in EVENT_SCHEMAS[event_type]:
            if field not in payload:
                raise ValidationError(f"payload 缺少字段: {field}")
        for field in _ID_FIELDS:
            value = payload.get(field)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValidationError(f"字段 {field} 必须是非空字符串")
                if ":" in value or "/" in value:
                    raise ValidationError(f"字段 {field} 不允许包含 ':' 或 '/'")
        if event_type == "project_registered":
            if not isinstance(payload["name"], str) or not payload["name"].strip():
                raise ValidationError("name 必须是非空字符串")
            if payload.get("owner") is not None and not isinstance(payload["owner"], str):
                raise ValidationError("owner 必须是字符串")
        elif event_type == "dependency_added":
            if not isinstance(payload["kind"], str) or not payload["kind"].strip():
                raise ValidationError("kind 必须是非空字符串")
        elif event_type == "holiday_registered":
            try:
                date.fromisoformat(payload["day"])
            except (ValueError, TypeError):
                raise ValidationError(f"day 非法: {payload['day']!r}") from None
        elif event_type == "rule_published":
            payload = normalize_rule(payload)
        elif event_type == "risk_raised":
            severity = payload["severity"]
            if not isinstance(severity, int) or isinstance(severity, bool) or not 1 <= severity <= 5:
                raise ValidationError("severity 必须是 1..5 的整数")
        out = {
            "message_id": message_id.strip(),
            "event_type": event_type,
            "occurred_at": occurred_at,
            "payload": payload,
        }
        actor = raw.get("actor")
        if actor is not None:
            if not isinstance(actor, str):
                raise ValidationError("actor 必须是字符串")
            out["actor"] = actor
        return out

    def _emit(self, event_type: str, payload: dict, actor: str, occurred_at=None) -> str:
        """登记类接口的便捷入口：构造事件并导入，返回 message_id。"""
        event = {
            "message_id": f"evt-{uuid.uuid4().hex}",
            "event_type": event_type,
            "payload": payload,
        }
        if occurred_at is not None:
            event["occurred_at"] = occurred_at
        if actor:
            event["actor"] = actor
        self.import_events([event], actor=actor or "system")
        return event["message_id"]

    def register_project(self, project_id, name, region, owner="", actor="api", occurred_at=None):
        payload = {"project_id": project_id, "name": name, "region": region}
        if owner:
            payload["owner"] = owner
        return self._emit("project_registered", payload, actor, occurred_at)

    def add_dependency(self, dependency_id, upstream, downstream, kind, actor="api", occurred_at=None):
        return self._emit(
            "dependency_added",
            {
                "dependency_id": dependency_id,
                "upstream": upstream,
                "downstream": downstream,
                "kind": kind,
            },
            actor,
            occurred_at,
        )

    def remove_dependency(self, dependency_id, actor="api", occurred_at=None):
        return self._emit("dependency_removed", {"dependency_id": dependency_id}, actor, occurred_at)

    def register_holiday(self, region, day, actor="api", occurred_at=None):
        return self._emit("holiday_registered", {"region": region, "day": day}, actor, occurred_at)

    def publish_rule(self, rule: dict, actor="api", occurred_at=None):
        return self._emit("rule_published", dict(rule), actor, occurred_at)

    def raise_risk(self, risk_id, project_id, category, severity, reason="", actor="api", occurred_at=None):
        payload = {
            "risk_id": risk_id,
            "project_id": project_id,
            "category": category,
            "severity": severity,
        }
        if reason:
            payload["reason"] = reason
        return self._emit("risk_raised", payload, actor, occurred_at)

    def revoke_risk(self, risk_id, reason="", actor="api", occurred_at=None):
        payload = {"risk_id": risk_id}
        if reason:
            payload["reason"] = reason
        return self._emit("risk_revoked", payload, actor, occurred_at)

    # ------------------------------------------------------------------
    # 登记信息查询
    # ------------------------------------------------------------------
    def list_projects(self) -> list[dict]:
        projects: dict[str, dict] = {}
        for row in self.db.query(
            "SELECT payload FROM events WHERE event_type='project_registered'"
            " ORDER BY occurred_at, message_id"
        ):
            payload = json.loads(row["payload"])
            projects[payload["project_id"]] = payload
        return sorted(projects.values(), key=lambda p: p["project_id"])

    def list_dependencies(self) -> list[dict]:
        deps: dict[str, dict] = {}
        for row in self.db.query(
            "SELECT event_type, payload FROM events"
            " WHERE event_type IN ('dependency_added','dependency_removed')"
            " ORDER BY occurred_at, message_id"
        ):
            payload = json.loads(row["payload"])
            if row["event_type"] == "dependency_added":
                deps[payload["dependency_id"]] = payload
            else:
                deps.pop(payload["dependency_id"], None)
        return sorted(deps.values(), key=lambda d: d["dependency_id"])

    def list_rules(self) -> list[dict]:
        rules: dict[int, dict] = {}
        for row in self.db.query(
            "SELECT payload FROM events WHERE event_type='rule_published'"
            " ORDER BY occurred_at, message_id"
        ):
            payload = json.loads(row["payload"])
            rules[payload["version"]] = payload
        return sorted(rules.values(), key=lambda r: r["version"])

    def list_holidays(self, region: str | None = None) -> list[dict]:
        days: set[tuple[str, str]] = set()
        for row in self.db.query("SELECT payload FROM events WHERE event_type='holiday_registered'"):
            payload = json.loads(row["payload"])
            if region is None or payload["region"] == region:
                days.add((payload["region"], payload["day"]))
        return [{"region": r, "day": d} for r, d in sorted(days)]

    # ------------------------------------------------------------------
    # 风险查询与影响分析
    # ------------------------------------------------------------------
    def get_risk(self, risk_key: str) -> dict:
        row = self.db.connection().execute(
            "SELECT * FROM risks WHERE risk_key=?", (risk_key,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"风险不存在: {risk_key}")
        return self._risk_view(dict(row))

    def list_risks(
        self,
        project_id=None,
        category=None,
        kind=None,
        status=None,
        effective_status=None,
        overdue=False,
    ) -> list[dict]:
        now = fmt(self.clock())
        out = []
        for row in self.db.query("SELECT * FROM risks ORDER BY risk_key"):
            view = self._risk_view(dict(row), now)
            if project_id and view["project_id"] != project_id:
                continue
            if category and view["category"] != category:
                continue
            if kind and view["kind"] != kind:
                continue
            if status and view["status"] != status:
                continue
            if effective_status and view["effective_status"] != effective_status:
                continue
            if overdue and not view["overdue"]:
                continue
            out.append(view)
        return out

    def overdue_risks(self) -> list[dict]:
        """已越过处置时限且仍暴露（未关闭、未在豁免期内）的风险。"""
        return self.list_risks(overdue=True)

    def project_impact(self, project_id: str) -> dict:
        """某项目受到的影响：自身基础风险 + 传播而来的派生风险。"""
        now = fmt(self.clock())
        risks = []
        for row in self.db.query(
            "SELECT * FROM risks WHERE project_id=? ORDER BY risk_key", (project_id,)
        ):
            view = self._risk_view(dict(row), now)
            if view["kind"] == "base":
                view["affected_projects"] = self._propagation_of(view["risk_key"])
            else:
                view["sources"] = self._resolve_basis(view["basis"])
            risks.append(view)
        return {"project_id": project_id, "risks": risks}

    def risk_propagation(self, risk_key: str) -> dict:
        """基础风险给出下游传播范围；派生风险给出上游依据来源。"""
        risk = self.get_risk(risk_key)
        if risk["kind"] == "base":
            return {
                "risk_key": risk_key,
                "kind": "base",
                "affected_projects": self._propagation_of(risk_key),
            }
        return {
            "risk_key": risk_key,
            "kind": "derived",
            "sources": self._resolve_basis(risk["basis"]),
        }

    def _propagation_of(self, base_risk_id: str) -> list[dict]:
        affected = []
        for row in self.db.query(
            "SELECT * FROM risks WHERE kind='derived' AND status='open' ORDER BY risk_key"
        ):
            for entry in json.loads(row["basis_json"]):
                if entry["base_risk_id"] == base_risk_id:
                    affected.append(
                        {
                            "project_id": row["project_id"],
                            "risk_key": row["risk_key"],
                            "severity": entry["severity"],
                            "hops": entry["hops"],
                            "deadline": row["deadline"],
                        }
                    )
        return affected

    def _resolve_basis(self, basis: list[dict]) -> list[dict]:
        resolved = []
        for entry in basis:
            row = self.db.connection().execute(
                "SELECT project_id, status FROM risks WHERE risk_key=?",
                (entry["base_risk_id"],),
            ).fetchone()
            item = dict(entry)
            if row is not None:
                item["base_project_id"] = row["project_id"]
                item["base_status"] = row["status"]
            resolved.append(item)
        return resolved

    def _risk_view(self, risk: dict, now: str | None = None) -> dict:
        now = now or fmt(self.clock())
        risk["basis"] = json.loads(risk.pop("basis_json"))
        exemption = self._active_exemption(risk["risk_key"], now)
        if risk["status"] == "closed":
            effective = "closed"
        elif exemption is not None:
            effective = "exempt"
        else:
            effective = "open"
        risk["effective_status"] = effective
        if exemption is not None:
            risk["exemption"] = exemption
        risk["overdue"] = (
            effective == "open"
            and risk["deadline"] is not None
            and risk["deadline"] < now[:10]
        )
        return risk

    def _active_exemption(self, risk_key: str, now: str) -> dict | None:
        row = self.db.connection().execute(
            "SELECT * FROM exemptions WHERE risk_key=? AND revoked_at IS NULL"
            " AND valid_from<=? AND valid_until>=? ORDER BY valid_until DESC LIMIT 1",
            (risk_key, now, now),
        ).fetchone()
        if row is None:
            return None
        return {
            "exemption_id": row["exemption_id"],
            "approved_by": row["approved_by"],
            "reason": row["reason"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
        }

    def risks_dump(self) -> list[dict]:
        """确定性快照，用于校验乱序/重复导入后状态一致。"""
        return [
            dict(r)
            for r in self.db.query(
                "SELECT risk_key, kind, project_id, category, severity, status, opened_at,"
                " closed_at, close_reason, deadline, basis_json, rule_version, updated_at"
                " FROM risks ORDER BY risk_key"
            )
        ]

    # ------------------------------------------------------------------
    # 豁免（人工确认，明确有效期）
    # ------------------------------------------------------------------
    def grant_exemption(self, risk_key, approved_by, reason, valid_from, valid_until) -> dict:
        self.get_risk(risk_key)  # 不存在则 404
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise ValidationError("approved_by 必填")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("reason 必填")
        try:
            start = fmt(valid_from)
            end = fmt(valid_until)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"豁免有效期非法: {exc}") from exc
        if end <= start:
            raise ValidationError("valid_until 必须晚于 valid_from")
        exemption_id = f"exm-{uuid.uuid4().hex[:12]}"
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO exemptions(exemption_id, risk_key, approved_by, reason,"
                " valid_from, valid_until, created_at) VALUES (?,?,?,?,?,?,?)",
                (exemption_id, risk_key, approved_by, reason, start, end, fmt(self.clock())),
            )
            self._audit(
                conn,
                approved_by,
                "exemption_granted",
                "exemption",
                exemption_id,
                {"risk_key": risk_key, "valid_from": start, "valid_until": end, "reason": reason},
            )
        return self.get_exemption(exemption_id)

    def revoke_exemption(self, exemption_id, actor="api") -> dict:
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM exemptions WHERE exemption_id=?", (exemption_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"豁免不存在: {exemption_id}")
            if row["revoked_at"] is None:
                conn.execute(
                    "UPDATE exemptions SET revoked_at=? WHERE exemption_id=?",
                    (fmt(self.clock()), exemption_id),
                )
                self._audit(
                    conn, actor, "exemption_revoked", "exemption", exemption_id,
                    {"risk_key": row["risk_key"]},
                )
        return self.get_exemption(exemption_id)

    def get_exemption(self, exemption_id: str) -> dict:
        row = self.db.connection().execute(
            "SELECT * FROM exemptions WHERE exemption_id=?", (exemption_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"豁免不存在: {exemption_id}")
        return dict(row)

    def list_exemptions(self, risk_key=None, active_only=False) -> list[dict]:
        sql = "SELECT * FROM exemptions"
        clauses, args = [], []
        if risk_key:
            clauses.append("risk_key=?")
            args.append(risk_key)
        if active_only:
            now = fmt(self.clock())
            clauses.append("revoked_at IS NULL AND valid_from<=? AND valid_until>=?")
            args.extend([now, now])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, exemption_id"
        return [dict(r) for r in self.db.query(sql, tuple(args))]

    # ------------------------------------------------------------------
    # 缓解措施与认领租约
    # ------------------------------------------------------------------
    def register_measure(self, risk_key, title, created_by, description="") -> dict:
        self.get_risk(risk_key)
        if not isinstance(title, str) or not title.strip():
            raise ValidationError("title 必填")
        if not isinstance(created_by, str) or not created_by.strip():
            raise ValidationError("created_by 必填")
        measure_id = f"msr-{uuid.uuid4().hex[:12]}"
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO measures(measure_id, risk_key, title, description, status,"
                " created_by, created_at) VALUES (?,?,?,?,'proposed',?,?)",
                (measure_id, risk_key, title.strip(), description or "", created_by, fmt(self.clock())),
            )
            self._audit(
                conn, created_by, "measure_registered", "measure", measure_id,
                {"risk_key": risk_key, "title": title.strip()},
            )
        return self.get_measure(measure_id)

    def claim_measure(self, measure_id, owner, ttl_seconds=3600) -> dict:
        """认领措施：同一责任方重复调用幂等返回；他人认领冲突返回 409。"""
        if not isinstance(owner, str) or not owner.strip():
            raise ValidationError("owner 必填")
        if (
            not isinstance(ttl_seconds, (int, float))
            or isinstance(ttl_seconds, bool)
            or not 1 <= ttl_seconds <= MAX_CLAIM_TTL_SECONDS
        ):
            raise ValidationError(f"ttl_seconds 必须在 1..{MAX_CLAIM_TTL_SECONDS} 之间")
        now_dt = self.clock()
        now = fmt(now_dt)
        expires = fmt(now_dt + timedelta(seconds=ttl_seconds))
        with self.db.tx() as conn:
            measure = conn.execute(
                "SELECT * FROM measures WHERE measure_id=?", (measure_id,)
            ).fetchone()
            if measure is None:
                raise NotFoundError(f"措施不存在: {measure_id}")
            if measure["status"] == "done":
                raise ConflictError("措施已完成，无法认领")
            self._expire_leases(conn, now, resource_id=measure_id)
            lease = conn.execute(
                "SELECT * FROM leases WHERE resource_type='measure' AND resource_id=?"
                " AND status='active'",
                (measure_id,),
            ).fetchone()
            if lease is not None:
                if lease["owner"] == owner:
                    return {"lease": dict(lease), "deduplicated": True}
                raise ConflictError(
                    f"措施已被 {lease['owner']} 认领", {"lease_id": lease["lease_id"]}
                )
            lease_id = f"lease-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO leases(lease_id, resource_type, resource_id, owner,"
                " acquired_at, expires_at, status) VALUES (?,?,?,?,?,?,'active')",
                (lease_id, "measure", measure_id, owner, now, expires),
            )
            conn.execute(
                "UPDATE measures SET status='claimed', assignee=?, claimed_at=?"
                " WHERE measure_id=?",
                (owner, now, measure_id),
            )
            self._audit(
                conn, owner, "lease_acquired", "lease", lease_id,
                {"measure_id": measure_id, "expires_at": expires},
            )
        return {"lease": self.get_lease(lease_id), "deduplicated": False}

    def release_measure(self, measure_id, owner) -> dict:
        with self.db.tx() as conn:
            lease = self._owned_active_lease(conn, measure_id, owner)
            conn.execute(
                "UPDATE leases SET status='released', released_at=? WHERE lease_id=?",
                (fmt(self.clock()), lease["lease_id"]),
            )
            conn.execute(
                "UPDATE measures SET status='proposed', assignee=NULL, claimed_at=NULL"
                " WHERE measure_id=?",
                (measure_id,),
            )
            self._audit(
                conn, owner, "lease_released", "lease", lease["lease_id"],
                {"measure_id": measure_id},
            )
        return self.get_measure(measure_id)

    def complete_measure(self, measure_id, owner) -> dict:
        with self.db.tx() as conn:
            lease = self._owned_active_lease(conn, measure_id, owner)
            conn.execute(
                "UPDATE leases SET status='released', released_at=? WHERE lease_id=?",
                (fmt(self.clock()), lease["lease_id"]),
            )
            conn.execute(
                "UPDATE measures SET status='done', done_at=? WHERE measure_id=?",
                (fmt(self.clock()), measure_id),
            )
            self._audit(
                conn, owner, "measure_completed", "measure", measure_id,
                {"lease_id": lease["lease_id"]},
            )
        return self.get_measure(measure_id)

    def _owned_active_lease(self, conn, measure_id: str, owner: str):
        measure = conn.execute(
            "SELECT * FROM measures WHERE measure_id=?", (measure_id,)
        ).fetchone()
        if measure is None:
            raise NotFoundError(f"措施不存在: {measure_id}")
        self._expire_leases(conn, fmt(self.clock()), resource_id=measure_id)
        lease = conn.execute(
            "SELECT * FROM leases WHERE resource_type='measure' AND resource_id=?"
            " AND status='active'",
            (measure_id,),
        ).fetchone()
        if lease is None:
            raise ConflictError("措施当前没有活跃租约")
        if lease["owner"] != owner:
            raise ConflictError(f"租约属于 {lease['owner']}，{owner} 无权操作")
        return lease

    def _expire_leases(self, conn, now: str, resource_id: str | None = None) -> None:
        sql = "SELECT * FROM leases WHERE status='active' AND expires_at<=?"
        args: list = [now]
        if resource_id is not None:
            sql += " AND resource_type='measure' AND resource_id=?"
            args.append(resource_id)
        for lease in conn.execute(sql, args).fetchall():
            conn.execute(
                "UPDATE leases SET status='expired' WHERE lease_id=?", (lease["lease_id"],)
            )
            if lease["resource_type"] == "measure":
                conn.execute(
                    "UPDATE measures SET status='proposed', assignee=NULL, claimed_at=NULL"
                    " WHERE measure_id=? AND status='claimed'",
                    (lease["resource_id"],),
                )
            self._audit(
                conn, "system", "lease_expired", "lease", lease["lease_id"],
                {"resource_type": lease["resource_type"], "resource_id": lease["resource_id"]},
            )

    def _sweep_expired_leases(self) -> None:
        with self.db.tx() as conn:
            self._expire_leases(conn, fmt(self.clock()))

    def get_measure(self, measure_id: str) -> dict:
        row = self.db.connection().execute(
            "SELECT * FROM measures WHERE measure_id=?", (measure_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"措施不存在: {measure_id}")
        return dict(row)

    def list_measures(self, risk_key=None, status=None) -> list[dict]:
        sql = "SELECT * FROM measures"
        clauses, args = [], []
        if risk_key:
            clauses.append("risk_key=?")
            args.append(risk_key)
        if status:
            clauses.append("status=?")
            args.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, measure_id"
        return [dict(r) for r in self.db.query(sql, tuple(args))]

    def get_lease(self, lease_id: str) -> dict:
        row = self.db.connection().execute(
            "SELECT * FROM leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"租约不存在: {lease_id}")
        return dict(row)

    def list_leases(self, status=None) -> list[dict]:
        if status:
            rows = self.db.query(
                "SELECT * FROM leases WHERE status=? ORDER BY acquired_at, lease_id", (status,)
            )
        else:
            rows = self.db.query("SELECT * FROM leases ORDER BY acquired_at, lease_id")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 升级与复盘
    # ------------------------------------------------------------------
    def escalate(self, risk_key, reason, created_by, level=None) -> dict:
        self.get_risk(risk_key)
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("reason 必填")
        if not isinstance(created_by, str) or not created_by.strip():
            raise ValidationError("created_by 必填")
        with self.db.tx() as conn:
            if level is None:
                row = conn.execute(
                    "SELECT MAX(level) AS top FROM escalations WHERE risk_key=?", (risk_key,)
                ).fetchone()
                level = (row["top"] or 0) + 1
            if not isinstance(level, int) or isinstance(level, bool) or level < 1:
                raise ValidationError("level 必须是 >= 1 的整数")
            escalation_id = f"esc-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO escalations(escalation_id, risk_key, level, reason, created_by,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (escalation_id, risk_key, level, reason, created_by, fmt(self.clock())),
            )
            self._audit(
                conn, created_by, "escalation_created", "escalation", escalation_id,
                {"risk_key": risk_key, "level": level, "reason": reason},
            )
            row = conn.execute(
                "SELECT * FROM escalations WHERE escalation_id=?", (escalation_id,)
            ).fetchone()
            return dict(row)

    def list_escalations(self, risk_key=None) -> list[dict]:
        if risk_key:
            rows = self.db.query(
                "SELECT * FROM escalations WHERE risk_key=? ORDER BY created_at, escalation_id",
                (risk_key,),
            )
        else:
            rows = self.db.query("SELECT * FROM escalations ORDER BY created_at, escalation_id")
        return [dict(r) for r in rows]

    def create_review(self, risk_key, summary, created_by, root_cause="", actions=None) -> dict:
        risk = self.get_risk(risk_key)
        if not isinstance(summary, str) or not summary.strip():
            raise ValidationError("summary 必填")
        if not isinstance(created_by, str) or not created_by.strip():
            raise ValidationError("created_by 必填")
        if actions is None:
            actions = []
        if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
            raise ValidationError("actions 必须是字符串列表")
        review_id = f"rev-{uuid.uuid4().hex[:12]}"
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO reviews(review_id, risk_key, summary, root_cause, actions_json,"
                " created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    review_id, risk_key, summary.strip(), root_cause or "",
                    json.dumps(actions, ensure_ascii=False), created_by, fmt(self.clock()),
                ),
            )
            self._audit(
                conn, created_by, "review_created", "review", review_id,
                {
                    "risk_key": risk_key,
                    "risk_status": risk["status"],
                    "risk_effective_status": risk["effective_status"],
                },
            )
        return self.get_review(review_id)

    def get_review(self, review_id: str) -> dict:
        row = self.db.connection().execute(
            "SELECT * FROM reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"复盘不存在: {review_id}")
        return self._review_view(dict(row))

    def list_reviews(self, risk_key=None) -> list[dict]:
        if risk_key:
            rows = self.db.query(
                "SELECT * FROM reviews WHERE risk_key=? ORDER BY created_at, review_id",
                (risk_key,),
            )
        else:
            rows = self.db.query("SELECT * FROM reviews ORDER BY created_at, review_id")
        return [self._review_view(dict(r)) for r in rows]

    @staticmethod
    def _review_view(review: dict) -> dict:
        review["actions"] = json.loads(review.pop("actions_json"))
        return review

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def _audit(self, conn, actor, action, entity_type, entity_id, detail) -> None:
        conn.execute(
            "INSERT INTO audit(ts, actor, action, entity_type, entity_id, detail)"
            " VALUES (?,?,?,?,?,?)",
            (
                fmt(self.clock()), actor, action, entity_type, entity_id,
                json.dumps(detail, sort_keys=True, ensure_ascii=False),
            ),
        )

    def list_audit(self, entity_type=None, entity_id=None, limit=500) -> list[dict]:
        sql = "SELECT * FROM audit"
        clauses, args = [], []
        if entity_type:
            clauses.append("entity_type=?")
            args.append(entity_type)
        if entity_id:
            clauses.append("entity_id=?")
            args.append(entity_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY audit_id LIMIT ?"
        args.append(int(limit))
        out = []
        for row in self.db.query(sql, tuple(args)):
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            out.append(item)
        return out
