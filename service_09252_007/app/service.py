"""合作办学风险联动应用服务。

职责：项目依赖登记、风险事件导入、按版本化规则计算传播范围与处置时限、
缓解措施认领（租约）、人工确认豁免（明确有效期）、升级与复盘。

一致性设计：
- 事件/撤销/升级/措施均以客户端幂等键去重，重复消息不改变结果；
- 撤销以事件标识为准而非到达顺序，撤销先于事件到达时记入待撤销表，
  事件补到后立即生效 —— 乱序与顺序到达的最终状态一致；
- 派生风险按 (类别, 项目) 聚合，可有多条独立依据；基础风险被撤销或解决后，
  仅关闭不再有任何有效依据的派生风险；
- 所有状态（含租约与审计）持久化于 SQLite，实例重建即可恢复。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from datetime import date, datetime, timedelta, timezone

from ..domain.calendars import BUILTIN_CALENDARS, WorkdayCalendar
from ..domain.rules import DEFAULT_RULE_VERSION, DEFAULT_RULES, validate_rules
from ..domain.severity import RANK, at_rank, next_up
from ..errors import Conflict, DomainError, InvalidInput, InvalidState, NotFound
from ..persistence.store import Store

BASE_PREFIX = "B:"
DERIVED_PREFIX = "D:"
LEASE_PREFIX = "measure:"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_STRICT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_id(value, field, strict=False):
    rx = _STRICT_ID_RE if strict else _ID_RE
    if not isinstance(value, str) or not rx.match(value):
        raise InvalidInput("invalid_id", f"{field} 非法: {value!r}")


def _parse_dt(value, field="datetime"):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise InvalidInput("invalid_datetime", f"无法解析{field}: {value!r}")
    else:
        raise InvalidInput("invalid_datetime", f"无法解析{field}: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def derived_risk_id(category, project_id):
    return f"{DERIVED_PREFIX}{category}:{project_id}"


class RiskService:
    """风险联动应用服务。clock 可注入以便确定性复现。"""

    def __init__(self, db_path=":memory:", clock=None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._store = Store(db_path)
        self._seed()

    # ---------- 基础设施 ----------

    def _now(self):
        return self._clock()

    def _now_iso(self):
        return _iso(self._clock())

    def _seed(self):
        with self._store.tx() as conn:
            for region, cal in BUILTIN_CALENDARS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO calendars(region, weekend, holidays) VALUES (?,?,?)",
                    (region, json.dumps(sorted(cal.weekend)),
                     json.dumps(sorted(d.isoformat() for d in cal.holidays))),
                )
            conn.execute(
                "INSERT OR IGNORE INTO rule_sets(version, payload, created_at) VALUES (?,?,?)",
                (DEFAULT_RULE_VERSION, json.dumps(DEFAULT_RULES, sort_keys=True), self._now_iso()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(k, v) VALUES ('active_rule_version', ?)",
                (DEFAULT_RULE_VERSION,),
            )

    @staticmethod
    def _one(conn, sql, args=()):
        return conn.execute(sql, args).fetchone()

    @staticmethod
    def _all(conn, sql, args=()):
        return conn.execute(sql, args).fetchall()

    def _audit(self, conn, actor, action, entity, entity_id, detail=None):
        conn.execute(
            "INSERT INTO audit(ts, actor, action, entity, entity_id, detail) VALUES (?,?,?,?,?,?)",
            (self._now_iso(), actor or "system", action, entity, entity_id,
             json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)),
        )

    def _calendar(self, conn, region):
        row = self._one(conn, "SELECT * FROM calendars WHERE region=?", (region,))
        if row is None:
            raise InvalidInput("unknown_region", f"未注册的地区日历: {region}")
        return WorkdayCalendar(
            weekend=frozenset(json.loads(row["weekend"])),
            holidays=frozenset(date.fromisoformat(s) for s in json.loads(row["holidays"])),
        )

    # ---------- 登记：日历 / 项目 / 依赖 / 规则 ----------

    def register_calendar(self, region, weekend, holidays=(), actor=None):
        """注册或覆盖一个地区工作日历。"""
        _check_id(region, "region", strict=True)
        if not isinstance(weekend, (list, tuple)) or \
                not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in weekend):
            raise InvalidInput("invalid_weekend", "weekend 必须是 0-6 的整数数组")
        try:
            hol = sorted({date.fromisoformat(h).isoformat() for h in holidays})
        except (ValueError, TypeError):
            raise InvalidInput("invalid_holidays", "holidays 必须是 YYYY-MM-DD 数组")
        weekend = sorted(set(weekend))
        with self._store.tx() as conn:
            conn.execute(
                "INSERT INTO calendars(region, weekend, holidays) VALUES (?,?,?) "
                "ON CONFLICT(region) DO UPDATE SET weekend=excluded.weekend, holidays=excluded.holidays",
                (region, json.dumps(weekend), json.dumps(hol)),
            )
            self._audit(conn, actor, "calendar_registered", "calendar", region,
                        {"weekend": weekend, "holidays": hol})
        return {"region": region, "weekend": weekend, "holidays": hol}

    def create_project(self, project_id, name, region, actor=None):
        _check_id(project_id, "project_id", strict=True)
        if not name:
            raise InvalidInput("invalid_name", "项目名称不能为空")
        with self._store.tx() as conn:
            self._calendar(conn, region)  # 校验地区已注册
            row = self._one(conn, "SELECT * FROM projects WHERE project_id=?", (project_id,))
            if row is not None:
                if row["name"] == name and row["region"] == region:
                    return dict(row)  # 幂等
                raise Conflict("project_exists", f"项目 {project_id} 已存在且属性不同")
            now = self._now_iso()
            conn.execute(
                "INSERT INTO projects(project_id, name, region, created_at) VALUES (?,?,?,?)",
                (project_id, name, region, now),
            )
            self._audit(conn, actor, "project_created", "project", project_id,
                        {"name": name, "region": region})
            return {"project_id": project_id, "name": name, "region": region, "created_at": now}

    def add_dependency(self, src, dst, dep_type, dep_id=None, actor=None):
        """登记依赖边 src -> dst：src 上的风险可沿该边传播到 dst。"""
        if src == dst:
            raise InvalidInput("invalid_dependency", "依赖不能指向自身")
        if not dep_type:
            raise InvalidInput("invalid_dependency", "dep_type 不能为空")
        dep_id = dep_id or f"{src}--{dst}--{dep_type}"
        _check_id(dep_id, "dep_id")
        with self._store.tx() as conn:
            for pid in (src, dst):
                if self._one(conn, "SELECT 1 FROM projects WHERE project_id=?", (pid,)) is None:
                    raise NotFound("project_not_found", f"项目不存在: {pid}")
            row = self._one(
                conn, "SELECT * FROM dependencies WHERE src=? AND dst=? AND dep_type=?",
                (src, dst, dep_type))
            if row is not None:
                return dict(row)  # 幂等
            if self._one(conn, "SELECT 1 FROM dependencies WHERE dep_id=?", (dep_id,)) is not None:
                raise Conflict("dependency_id_in_use", f"dep_id {dep_id} 已被占用")
            now = self._now_iso()
            conn.execute(
                "INSERT INTO dependencies(dep_id, src, dst, dep_type, created_at) VALUES (?,?,?,?,?)",
                (dep_id, src, dst, dep_type, now),
            )
            self._audit(conn, actor, "dependency_added", "dependency", dep_id,
                        {"src": src, "dst": dst, "dep_type": dep_type})
            return {"dep_id": dep_id, "src": src, "dst": dst, "dep_type": dep_type,
                    "created_at": now}

    def register_rules(self, version, payload, activate=True, actor=None):
        """注册新版本规则。版本不可变：同版本同内容幂等，同版本不同内容冲突。"""
        _check_id(version, "version", strict=True)
        normalized = validate_rules(payload)
        with self._store.tx() as conn:
            row = self._one(conn, "SELECT * FROM rule_sets WHERE version=?", (version,))
            if row is not None:
                if json.loads(row["payload"]) == normalized:
                    return {"version": version, "rules": normalized,
                            "active": self._active_version(conn) == version}
                raise Conflict("rule_version_exists", f"规则版本 {version} 已存在且内容不同")
            conn.execute(
                "INSERT INTO rule_sets(version, payload, created_at) VALUES (?,?,?)",
                (version, json.dumps(normalized, sort_keys=True), self._now_iso()),
            )
            if activate:
                conn.execute("UPDATE meta SET v=? WHERE k='active_rule_version'", (version,))
            self._audit(conn, actor, "rules_registered", "rule_set", version,
                        {"activate": activate})
            return {"version": version, "rules": normalized, "active": activate}

    def _active_version(self, conn):
        return self._one(conn, "SELECT v FROM meta WHERE k='active_rule_version'")["v"]

    def _rules(self, conn, version):
        row = self._one(conn, "SELECT payload FROM rule_sets WHERE version=?", (version,))
        if row is None:
            raise NotFound("rules_not_found", f"规则版本不存在: {version}")
        return json.loads(row["payload"])

    def active_rules(self):
        with self._store.read() as conn:
            version = self._active_version(conn)
            return {"version": version, "rules": self._rules(conn, version)}

    # ---------- 风险事件：导入与撤销 ----------

    def import_events(self, events, actor=None):
        """批量导入风险事件。逐事件独立事务，返回逐条结果。

        以 event_id 幂等：重复导入返回 duplicate；同 id 不同载荷返回错误。
        若该事件已有待撤销记录（乱序到达），导入后立即按撤销处理。
        """
        if not isinstance(events, list):
            raise InvalidInput("invalid_batch", "events 必须是数组")
        results = []
        for ev in events:
            eid = ev.get("event_id") if isinstance(ev, dict) else None
            try:
                with self._store.tx() as conn:
                    results.append(self._import_one(conn, ev, actor))
            except DomainError as exc:
                results.append({"event_id": eid, "status": "error", "error": exc.to_dict()})
        return results

    def _import_one(self, conn, ev, actor):
        if not isinstance(ev, dict):
            raise InvalidInput("invalid_event", "事件必须是对象")
        for field in ("event_id", "project_id", "category", "severity", "occurred_at"):
            if not ev.get(field):
                raise InvalidInput("missing_field", f"事件缺少字段: {field}")
        event_id = ev["event_id"]
        _check_id(event_id, "event_id")
        _check_id(ev["category"], "category", strict=True)
        severity = ev["severity"]
        if severity not in RANK:
            raise InvalidInput("unknown_severity", f"未知级别: {severity}")
        occurred = _parse_dt(ev["occurred_at"], "occurred_at")
        version = self._active_version(conn)
        rules = self._rules(conn, version)
        if ev["category"] not in rules["propagation"]:
            raise InvalidInput("unknown_category", f"规则未覆盖的类别: {ev['category']}")
        project = self._one(conn, "SELECT * FROM projects WHERE project_id=?", (ev["project_id"],))
        if project is None:
            raise NotFound("project_not_found", f"项目不存在: {ev['project_id']}")

        canonical = json.dumps(
            {"project_id": ev["project_id"], "category": ev["category"],
             "severity": severity, "occurred_at": _iso(occurred)},
            sort_keys=True, ensure_ascii=False)
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = self._one(conn, "SELECT * FROM events WHERE event_id=?", (event_id,))
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise Conflict("event_payload_mismatch",
                               f"事件 {event_id} 已存在且载荷不同")
            return {"event_id": event_id, "status": "duplicate"}

        pending = self._one(
            conn, "SELECT * FROM pending_revocations WHERE event_id=?", (event_id,))
        now = self._now_iso()
        conn.execute(
            "INSERT INTO events(event_id, project_id, category, severity, occurred_at, note,"
            " payload_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, ev["project_id"], ev["category"], severity, _iso(occurred),
             ev.get("note"), payload_hash, now),
        )
        self._audit(conn, actor, "event_imported", "event", event_id,
                    {"project_id": ev["project_id"], "category": ev["category"],
                     "severity": severity, "rule_version": version})

        # 基础风险
        cal = self._calendar(conn, project["region"])
        deadline = cal.add(occurred.date(), rules["deadline_workdays"][severity]).isoformat()
        base_id = BASE_PREFIX + event_id
        conn.execute(
            "INSERT INTO risks(risk_id, kind, project_id, category, base_severity, severity,"
            " status, deadline, rule_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (base_id, "base", ev["project_id"], ev["category"], severity, severity,
             "open", deadline, version, now),
        )
        self._audit(conn, actor, "risk_opened", "risk", base_id,
                    {"kind": "base", "deadline": deadline})

        impacts = self._propagate(conn, event_id, ev["project_id"], ev["category"],
                                  severity, occurred, version, rules, actor)

        if pending is not None:  # 乱序：撤销先于事件到达
            self._apply_revocation(conn, event_id, pending["reason"], actor,
                                   pending["revoked_at"])
        return {"event_id": event_id, "status": "imported",
                "revoked": pending is not None, "rule_version": version,
                "impacts": impacts}

    def _propagate(self, conn, event_id, src_project, category, severity, occurred,
                   version, rules, actor):
        """按规则沿依赖图传播，返回受影响项目列表。环按路径截断，保证终止。"""
        cfg = rules["propagation"][category]
        edge_types = set(cfg["edge_types"])
        decay = cfg["decay_per_hop"]
        max_depth = cfg["max_depth"]

        graph = {}
        for row in self._all(conn, "SELECT src, dst, dep_type FROM dependencies"):
            graph.setdefault(row["src"], []).append((row["dst"], row["dep_type"]))

        # BFS，按项目保留最高残余级别（同级取先到即最短路径）
        best = {}
        queue = deque([(src_project, RANK[severity], [src_project])])
        while queue:
            project, sev_rank, path = queue.popleft()
            if len(path) - 1 >= max_depth:
                continue
            for dst, dep_type in graph.get(project, ()):
                if dep_type not in edge_types or dst in path:
                    continue
                next_rank = sev_rank - decay
                if next_rank < 1:
                    continue
                current = best.get(dst)
                if current is not None and current[0] >= next_rank:
                    continue
                best[dst] = (next_rank, path + [dst])
                queue.append((dst, next_rank, path + [dst]))

        impacts = []
        now = self._now_iso()
        for project_id, (sev_rank, path) in sorted(best.items()):
            risk_id = derived_risk_id(category, project_id)
            row = self._one(conn, "SELECT * FROM risks WHERE risk_id=?", (risk_id,))
            if row is None:
                conn.execute(
                    "INSERT INTO risks(risk_id, kind, project_id, category, base_severity,"
                    " severity, status, rule_version, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (risk_id, "derived", project_id, category, at_rank(sev_rank),
                     at_rank(sev_rank), "open", version, now),
                )
                self._audit(conn, actor, "risk_opened", "risk", risk_id,
                            {"kind": "derived", "basis_event": event_id})
            elif row["status"] != "open":
                conn.execute(
                    "UPDATE risks SET status='open', close_reason=NULL, closed_at=NULL"
                    " WHERE risk_id=?", (risk_id,))
                self._audit(conn, actor, "risk_reopened", "risk", risk_id,
                            {"basis_event": event_id})
            conn.execute(
                "INSERT OR REPLACE INTO risk_bases(risk_id, event_id, decayed_severity, path)"
                " VALUES (?,?,?,?)",
                (risk_id, event_id, at_rank(sev_rank), json.dumps(path)),
            )
            self._refresh_derived(conn, risk_id)
            fresh = self._one(conn, "SELECT * FROM risks WHERE risk_id=?", (risk_id,))
            impacts.append({"project_id": project_id, "risk_id": risk_id,
                            "severity": fresh["severity"], "deadline": fresh["deadline"],
                            "path": path})
        return impacts

    def revoke_event(self, event_id, reason=None, actor=None, revoked_at=None):
        """撤销风险事件。幂等；事件未到达时登记待撤销（乱序安全）。"""
        with self._store.tx() as conn:
            ev = self._one(conn, "SELECT * FROM events WHERE event_id=?", (event_id,))
            if ev is None:
                conn.execute(
                    "INSERT OR IGNORE INTO pending_revocations(event_id, reason, revoked_at)"
                    " VALUES (?,?,?)",
                    (event_id, reason, revoked_at or self._now_iso()),
                )
                self._audit(conn, actor, "revocation_pending", "event", event_id,
                            {"reason": reason})
                return {"event_id": event_id, "status": "pending"}
            if ev["revoked"]:
                return {"event_id": event_id, "status": "already_revoked"}
            self._apply_revocation(conn, event_id, reason, actor,
                                   revoked_at or self._now_iso())
            return {"event_id": event_id, "status": "revoked"}

    def _apply_revocation(self, conn, event_id, reason, actor, revoked_at):
        conn.execute(
            "UPDATE events SET revoked=1, revoked_at=?, revoke_reason=? WHERE event_id=?",
            (revoked_at, reason, event_id),
        )
        # 待撤销记录已生效，清除后两种到达顺序的最终状态完全一致
        conn.execute("DELETE FROM pending_revocations WHERE event_id=?", (event_id,))
        self._audit(conn, actor, "event_revoked", "event", event_id, {"reason": reason})
        base_id = BASE_PREFIX + event_id
        base = self._one(conn, "SELECT * FROM risks WHERE risk_id=?", (base_id,))
        if base is not None and base["status"] == "open":
            conn.execute(
                "UPDATE risks SET status='closed', close_reason='revoked', closed_at=?"
                " WHERE risk_id=?", (self._now_iso(), base_id))
            self._audit(conn, actor, "risk_revoked", "risk", base_id, {"reason": reason})
        # 只关闭失去全部有效依据的派生风险
        for row in self._all(conn, "SELECT risk_id FROM risk_bases WHERE event_id=?",
                             (event_id,)):
            self._refresh_derived(conn, row["risk_id"])

    # ---------- 派生风险聚合 ----------

    def _active_bases(self, conn, risk_id):
        """有效依据：源事件未撤销且其基础风险仍在处置中。"""
        rows = self._all(conn, """
            SELECT rb.event_id, rb.decayed_severity, e.occurred_at, e.revoked,
                   br.status AS base_status
            FROM risk_bases rb
            JOIN events e ON e.event_id = rb.event_id
            JOIN risks br ON br.risk_id = ? || rb.event_id
            WHERE rb.risk_id=?""", (BASE_PREFIX, risk_id))
        return [r for r in rows if not r["revoked"] and r["base_status"] == "open"]

    def _max_escalation_rank(self, conn, risk_id):
        ranks = [RANK[r["to_severity"]] for r in self._all(
            conn, "SELECT to_severity FROM escalations WHERE risk_id=?", (risk_id,))]
        return max(ranks, default=0)

    def _refresh_derived(self, conn, risk_id):
        """按有效依据重算派生风险：无有效依据则关闭，否则聚合级别与最早时限。"""
        risk = self._one(conn, "SELECT * FROM risks WHERE risk_id=?", (risk_id,))
        active = self._active_bases(conn, risk_id)
        if not active:
            if risk["status"] == "open":
                conn.execute(
                    "UPDATE risks SET status='closed', close_reason='no_active_basis',"
                    " closed_at=? WHERE risk_id=?", (self._now_iso(), risk_id))
                self._audit(conn, "system", "risk_auto_closed", "risk", risk_id,
                            {"reason": "no_active_basis"})
            return
        rules = self._rules(conn, risk["rule_version"])
        project = self._one(conn, "SELECT * FROM projects WHERE project_id=?",
                            (risk["project_id"],))
        cal = self._calendar(conn, project["region"])
        base_rank = max(RANK[b["decayed_severity"]] for b in active)
        eff_rank = max(base_rank, self._max_escalation_rank(conn, risk_id))
        days = rules["deadline_workdays"][at_rank(eff_rank)]
        deadline = min(
            cal.add(_parse_dt(b["occurred_at"]).date(), days) for b in active
        ).isoformat()
        conn.execute(
            "UPDATE risks SET base_severity=?, severity=?, deadline=? WHERE risk_id=?",
            (at_rank(base_rank), at_rank(eff_rank), deadline, risk_id),
        )

    def _refresh_base(self, conn, risk_id):
        """升级后重算基础风险的生效级别与时限。"""
        risk = self._one(conn, "SELECT * FROM risks WHERE risk_id=?", (risk_id,))
        ev = self._one(conn, "SELECT * FROM events WHERE event_id=?",
                       (risk_id[len(BASE_PREFIX):],))
        rules = self._rules(conn, risk["rule_version"])
        project = self._one(conn, "SELECT * FROM projects WHERE project_id=?",
                            (risk["project_id"],))
        cal = self._calendar(conn, project["region"])
        eff_rank = max(RANK[ev["severity"]], self._max_escalation_rank(conn, risk_id))
        days = rules["deadline_workdays"][at_rank(eff_rank)]
        deadline = cal.add(_parse_dt(ev["occurred_at"]).date(), days).isoformat()
        conn.execute(
            "UPDATE risks SET base_severity=?, severity=?, deadline=? WHERE risk_id=?",
            (ev["severity"], at_rank(eff_rank), deadline, risk_id),
        )

    def _refresh_risk(self, conn, risk_id):
        kind = self._one(conn, "SELECT kind FROM risks WHERE risk_id=?", (risk_id,))["kind"]
        if kind == "base":
            self._refresh_base(conn, risk_id)
        else:
            self._refresh_derived(conn, risk_id)

    # ---------- 风险处置：解决 / 升级 / 复盘 ----------

    def resolve_risk(self, risk_id, actor=None, note=None):
        """人工标记风险已处置。基础风险解决后，失去依据的派生风险随之关闭。"""
        with self._store.tx() as conn:
            risk = self._one(conn, "SELECT * FROM risks WHERE risk_id=?", (risk_id,))
            if risk is None:
                raise NotFound("risk_not_found", f"风险不存在: {risk_id}")
            if risk["status"] == "resolved":
                return self._risk_view(conn, risk_id)  # 幂等
            if risk["status"] != "open":
                raise InvalidState("risk_not_open", f"风险 {risk_id} 当前状态不可解决")
            conn.execute(
                "UPDATE risks SET status='resolved', close_reason='resolved', closed_at=?"
                " WHERE risk_id=?", (self._now_iso(), risk_id))
            self._audit(conn, actor, "risk_resolved", "risk", risk_id, {"note": note})
            if risk["kind"] == "base":
                event_id = risk_id[len(BASE_PREFIX):]
                for row in self._all(conn, "SELECT risk_id FROM risk_bases WHERE event_id=?",
                                     (event_id,)):
                    self._refresh_derived(conn, row["risk_id"])
            return self._risk_view(conn, risk_id)

    def escalate(self, risk_id, esc_id, actor=None, reason=None, to_severity=None):
        """升级风险。esc_id 幂等；生效级别取原始级别与所有升级目标的最大值。"""
        if not esc_id:
            raise InvalidInput("missing_field", "esc_id 不能为空")
        if to_severity is not None and to_severity not in RANK:
            raise InvalidInput("unknown_severity", f"未知级别: {to_severity}")
        with self._store.tx() as conn:
            risk = self._one(conn, "SELECT * FROM risks WHERE risk_id=?", (risk_id,))
            if risk is None:
                raise NotFound("risk_not_found", f"风险不存在: {risk_id}")
            existing = self._one(conn, "SELECT * FROM escalations WHERE esc_id=?", (esc_id,))
            if existing is not None:
                if existing["risk_id"] != risk_id:
                    raise Conflict("escalation_id_in_use", f"esc_id {esc_id} 已被占用")
                return self._risk_view(conn, risk_id)  # 幂等
            if to_severity is not None:
                target = to_severity
            else:
                target = next_up(risk["severity"])
                if target is None:
                    raise InvalidState("already_max_severity",
                                       f"风险 {risk_id} 已是最高级别")
            conn.execute(
                "INSERT INTO escalations(esc_id, risk_id, to_severity, reason, actor,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (esc_id, risk_id, target, reason, actor, self._now_iso()),
            )
            self._audit(conn, actor, "risk_escalated", "risk", risk_id,
                        {"esc_id": esc_id, "to_severity": target, "reason": reason})
            self._refresh_risk(conn, risk_id)
            return self._risk_view(conn, risk_id)

    def create_postmortem(self, risk_id, pm_id, root_cause=None, timeline=None,
                          lessons=None, created_by=None):
        """对已关闭/已解决的风险登记复盘。每条风险仅一份；pm_id 幂等。"""
        if not pm_id:
            raise InvalidInput("missing_field", "pm_id 不能为空")
        with self._store.tx() as conn:
            risk = self._one(conn, "SELECT * FROM risks WHERE risk_id=?", (risk_id,))
            if risk is None:
                raise NotFound("risk_not_found", f"风险不存在: {risk_id}")
            if risk["status"] == "open":
                raise InvalidState("risk_still_open", "风险仍在处置中，不能复盘")
            existing = self._one(
                conn, "SELECT * FROM postmortems WHERE risk_id=?", (risk_id,))
            if existing is not None:
                if existing["pm_id"] == pm_id:
                    return dict(existing)  # 幂等
                raise Conflict("postmortem_exists", f"风险 {risk_id} 已有复盘")
            if self._one(conn, "SELECT 1 FROM postmortems WHERE pm_id=?", (pm_id,)):
                raise Conflict("postmortem_id_in_use", f"pm_id {pm_id} 已被占用")
            now = self._now_iso()
            conn.execute(
                "INSERT INTO postmortems(pm_id, risk_id, root_cause, timeline, lessons,"
                " created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (pm_id, risk_id, root_cause, timeline, lessons, created_by, now),
            )
            self._audit(conn, created_by, "postmortem_created", "risk", risk_id,
                        {"pm_id": pm_id})
            return {"pm_id": pm_id, "risk_id": risk_id, "root_cause": root_cause,
                    "timeline": timeline, "lessons": lessons, "created_by": created_by,
                    "created_at": now}

    def get_postmortem(self, risk_id):
        with self._store.read() as conn:
            row = self._one(conn, "SELECT * FROM postmortems WHERE risk_id=?", (risk_id,))
            if row is None:
                raise NotFound("postmortem_not_found", f"风险 {risk_id} 没有复盘")
            return dict(row)

    # ---------- 缓解措施与租约 ----------

    def create_measure(self, measure_id, risk_id, title, created_by=None):
        _check_id(measure_id, "measure_id")
        if not title:
            raise InvalidInput("invalid_title", "措施标题不能为空")
        with self._store.tx() as conn:
            if self._one(conn, "SELECT 1 FROM risks WHERE risk_id=?", (risk_id,)) is None:
                raise NotFound("risk_not_found", f"风险不存在: {risk_id}")
            row = self._one(conn, "SELECT * FROM measures WHERE measure_id=?", (measure_id,))
            if row is not None:
                if row["risk_id"] == risk_id and row["title"] == title:
                    return self._measure_view(conn, row)  # 幂等
                raise Conflict("measure_exists", f"措施 {measure_id} 已存在且内容不同")
            now = self._now_iso()
            conn.execute(
                "INSERT INTO measures(measure_id, risk_id, title, created_by, created_at)"
                " VALUES (?,?,?,?,?)",
                (measure_id, risk_id, title, created_by, now),
            )
            self._audit(conn, created_by, "measure_created", "measure", measure_id,
                        {"risk_id": risk_id, "title": title})
            return self._measure_view(conn, self._one(
                conn, "SELECT * FROM measures WHERE measure_id=?", (measure_id,)))

    def claim_measure(self, measure_id, owner, token, ttl_seconds=3600):
        """认领措施：以租约实现，token 为幂等键。租约持久化，重启后仍有效。"""
        if not owner or not token:
            raise InvalidInput("missing_field", "owner 与 token 不能为空")
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError):
            raise InvalidInput("invalid_ttl", "ttl_seconds 必须是正整数")
        if ttl <= 0:
            raise InvalidInput("invalid_ttl", "ttl_seconds 必须是正整数")
        with self._store.tx() as conn:
            measure = self._one(conn, "SELECT * FROM measures WHERE measure_id=?",
                                (measure_id,))
            if measure is None:
                raise NotFound("measure_not_found", f"措施不存在: {measure_id}")
            if measure["status"] != "open":
                raise Conflict("measure_not_open", f"措施 {measure_id} 当前不可认领")
            resource = LEASE_PREFIX + measure_id
            lease = self._one(conn, "SELECT * FROM leases WHERE resource=?", (resource,))
            now_iso = self._now_iso()
            if lease is not None:
                if lease["expires_at"] > now_iso:
                    if lease["token"] == token:
                        if lease["owner"] != owner:
                            raise Conflict("token_owner_mismatch",
                                           "token 与认领人不匹配")
                        return {"measure_id": measure_id, "status": "already_claimed",
                                "lease": dict(lease)}  # 幂等重试
                    raise Conflict("lease_held", "措施已被他人认领",
                                   {"owner": lease["owner"], "expires_at": lease["expires_at"]})
                # 租约已过期，回收
                conn.execute("DELETE FROM leases WHERE resource=?", (resource,))
                self._audit(conn, "system", "lease_expired", "measure", measure_id,
                            {"previous_owner": lease["owner"]})
            expires = _iso(self._now() + timedelta(seconds=ttl))
            conn.execute(
                "INSERT INTO leases(resource, owner, token, acquired_at, expires_at)"
                " VALUES (?,?,?,?,?)",
                (resource, owner, token, now_iso, expires),
            )
            self._audit(conn, owner, "measure_claimed", "measure", measure_id,
                        {"owner": owner, "expires_at": expires})
            return {"measure_id": measure_id, "status": "claimed",
                    "lease": {"resource": resource, "owner": owner, "token": token,
                              "acquired_at": now_iso, "expires_at": expires}}

    def _held_lease(self, conn, measure_id, token):
        """校验调用方持有有效租约，返回租约行。"""
        lease = self._one(conn, "SELECT * FROM leases WHERE resource=?",
                          (LEASE_PREFIX + measure_id,))
        if lease is None:
            raise Conflict("not_claimed", f"措施 {measure_id} 未被认领")
        if lease["expires_at"] <= self._now_iso():
            raise Conflict("lease_expired", f"措施 {measure_id} 的认领已过期")
        if lease["token"] != token:
            raise Conflict("token_mismatch", "认领凭证不匹配")
        return lease

    def heartbeat(self, measure_id, token, ttl_seconds=3600):
        """续租：延长认领有效期。"""
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError):
            raise InvalidInput("invalid_ttl", "ttl_seconds 必须是正整数")
        if ttl <= 0:
            raise InvalidInput("invalid_ttl", "ttl_seconds 必须是正整数")
        with self._store.tx() as conn:
            self._held_lease(conn, measure_id, token)
            expires = _iso(self._now() + timedelta(seconds=ttl))
            conn.execute("UPDATE leases SET expires_at=? WHERE resource=?",
                         (expires, LEASE_PREFIX + measure_id))
            self._audit(conn, "system", "lease_renewed", "measure", measure_id,
                        {"expires_at": expires})
            return {"measure_id": measure_id, "expires_at": expires}

    def release_measure(self, measure_id, token):
        """主动释放认领。"""
        with self._store.tx() as conn:
            lease = self._one(conn, "SELECT * FROM leases WHERE resource=?",
                              (LEASE_PREFIX + measure_id,))
            if lease is None:
                return {"measure_id": measure_id, "status": "released"}  # 幂等
            if lease["token"] != token:
                raise Conflict("token_mismatch", "认领凭证不匹配")
            conn.execute("DELETE FROM leases WHERE resource=?",
                         (LEASE_PREFIX + measure_id,))
            self._audit(conn, lease["owner"], "lease_released", "measure", measure_id, {})
            return {"measure_id": measure_id, "status": "released"}

    def complete_measure(self, measure_id, token, actor=None):
        """凭有效租约完成措施。"""
        with self._store.tx() as conn:
            measure = self._one(conn, "SELECT * FROM measures WHERE measure_id=?",
                                (measure_id,))
            if measure is None:
                raise NotFound("measure_not_found", f"措施不存在: {measure_id}")
            if measure["status"] != "open":
                raise Conflict("measure_not_open", f"措施 {measure_id} 当前不可完成")
            lease = self._held_lease(conn, measure_id, token)
            now = self._now_iso()
            conn.execute(
                "UPDATE measures SET status='done', completed_at=?, completed_by=?"
                " WHERE measure_id=?", (now, actor or lease["owner"], measure_id))
            conn.execute("DELETE FROM leases WHERE resource=?",
                         (LEASE_PREFIX + measure_id,))
            self._audit(conn, actor or lease["owner"], "measure_completed", "measure",
                        measure_id, {})
            return self._measure_view(conn, self._one(
                conn, "SELECT * FROM measures WHERE measure_id=?", (measure_id,)))

    def _measure_view(self, conn, row):
        view = dict(row)
        lease = self._one(conn, "SELECT * FROM leases WHERE resource=?",
                          (LEASE_PREFIX + row["measure_id"],))
        if lease is None:
            view["lease"] = None
        else:
            view["lease"] = dict(lease)
            view["lease"]["expired"] = lease["expires_at"] <= self._now_iso()
        return view

    def get_measure(self, measure_id):
        with self._store.read() as conn:
            row = self._one(conn, "SELECT * FROM measures WHERE measure_id=?", (measure_id,))
            if row is None:
                raise NotFound("measure_not_found", f"措施不存在: {measure_id}")
            return self._measure_view(conn, row)

    # ---------- 豁免（人工确认 + 明确有效期） ----------

    def create_exemption(self, exemption_id, confirmed_by, reason, valid_from,
                         valid_until, risk_id=None, project_id=None, category=None,
                         actor=None):
        """登记豁免。必须人工确认（confirmed_by）且带明确有效期。"""
        _check_id(exemption_id, "exemption_id")
        if not confirmed_by:
            raise InvalidInput("missing_confirmation", "豁免必须有人工确认人")
        if not reason:
            raise InvalidInput("missing_reason", "豁免必须说明理由")
        vf = _parse_dt(valid_from, "valid_from")
        vu = _parse_dt(valid_until, "valid_until")
        if not vf < vu:
            raise InvalidInput("invalid_validity_window", "valid_from 必须早于 valid_until")
        by_risk = risk_id is not None
        by_scope = project_id is not None and category is not None
        if by_risk == by_scope:
            raise InvalidInput("invalid_scope",
                               "豁免须指定 risk_id，或 project_id 与 category 二者")
        with self._store.tx() as conn:
            if by_risk:
                if self._one(conn, "SELECT 1 FROM risks WHERE risk_id=?", (risk_id,)) is None:
                    raise NotFound("risk_not_found", f"风险不存在: {risk_id}")
            else:
                _check_id(category, "category", strict=True)
                if self._one(conn, "SELECT 1 FROM projects WHERE project_id=?",
                             (project_id,)) is None:
                    raise NotFound("project_not_found", f"项目不存在: {project_id}")
            row = self._one(conn, "SELECT * FROM exemptions WHERE exemption_id=?",
                            (exemption_id,))
            payload = {"risk_id": risk_id, "project_id": project_id, "category": category,
                       "reason": reason, "confirmed_by": confirmed_by,
                       "valid_from": _iso(vf), "valid_until": _iso(vu)}
            if row is not None:
                same = all(row[k] == v for k, v in payload.items())
                if same:
                    return dict(row)  # 幂等
                raise Conflict("exemption_exists", f"豁免 {exemption_id} 已存在且内容不同")
            now = self._now_iso()
            conn.execute(
                "INSERT INTO exemptions(exemption_id, risk_id, project_id, category, reason,"
                " confirmed_by, valid_from, valid_until, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (exemption_id, risk_id, project_id, category, reason, confirmed_by,
                 _iso(vf), _iso(vu), now),
            )
            self._audit(conn, actor or confirmed_by, "exemption_created", "exemption",
                        exemption_id, payload)
            return {"exemption_id": exemption_id, "revoked": 0, "created_at": now, **payload}

    def revoke_exemption(self, exemption_id, actor=None):
        with self._store.tx() as conn:
            row = self._one(conn, "SELECT * FROM exemptions WHERE exemption_id=?",
                            (exemption_id,))
            if row is None:
                raise NotFound("exemption_not_found", f"豁免不存在: {exemption_id}")
            if row["revoked"]:
                return {"exemption_id": exemption_id, "status": "already_revoked"}
            conn.execute("UPDATE exemptions SET revoked=1 WHERE exemption_id=?",
                         (exemption_id,))
            self._audit(conn, actor, "exemption_revoked", "exemption", exemption_id, {})
            return {"exemption_id": exemption_id, "status": "revoked"}

    def _effective_status(self, conn, risk, now_iso):
        """生效状态：open 且被有效豁免覆盖时视为 exempted。"""
        if risk["status"] != "open":
            return risk["status"]
        covering = self._one(conn, """
            SELECT 1 FROM exemptions
            WHERE revoked=0 AND valid_from<=? AND valid_until>?
              AND (risk_id=? OR (project_id=? AND category=?)) LIMIT 1""",
            (now_iso, now_iso, risk["risk_id"], risk["project_id"], risk["category"]))
        return "exempted" if covering else "open"

    # ---------- 查询：影响 / 汇总 / 审计 ----------

    def _risk_view(self, conn, risk_id, now_iso=None):
        risk = self._one(conn, "SELECT * FROM risks WHERE risk_id=?", (risk_id,))
        if risk is None:
            raise NotFound("risk_not_found", f"风险不存在: {risk_id}")
        now_iso = now_iso or self._now_iso()
        out = dict(risk)
        out["effective_status"] = self._effective_status(conn, risk, now_iso)
        if risk["kind"] == "derived":
            bases = self._all(conn, """
                SELECT rb.event_id, rb.decayed_severity, rb.path, e.revoked AS event_revoked,
                       e.project_id AS source_project, br.status AS base_status
                FROM risk_bases rb
                JOIN events e ON e.event_id = rb.event_id
                JOIN risks br ON br.risk_id = ? || rb.event_id
                WHERE rb.risk_id=? ORDER BY rb.event_id""", (BASE_PREFIX, risk_id))
            out["bases"] = []
            for b in bases:
                item = dict(b)
                item["path"] = json.loads(item["path"])
                item["active"] = (not item["event_revoked"]) and item["base_status"] == "open"
                out["bases"].append(item)
        else:
            event_id = risk_id[len(BASE_PREFIX):]
            out["event_id"] = event_id
            out["derived_risks"] = [r["risk_id"] for r in self._all(
                conn, "SELECT risk_id FROM risk_bases WHERE event_id=? ORDER BY risk_id",
                (event_id,))]
        out["escalations"] = [dict(r) for r in self._all(
            conn, "SELECT * FROM escalations WHERE risk_id=? ORDER BY created_at, esc_id",
            (risk_id,))]
        out["measures"] = [self._measure_view(conn, r) for r in self._all(
            conn, "SELECT * FROM measures WHERE risk_id=? ORDER BY measure_id", (risk_id,))]
        out["exemptions"] = [dict(r) for r in self._all(conn, """
            SELECT * FROM exemptions WHERE revoked=0
              AND (risk_id=? OR (project_id=? AND category=?)) ORDER BY exemption_id""",
            (risk_id, risk["project_id"], risk["category"]))]
        return out

    def get_risk(self, risk_id):
        with self._store.read() as conn:
            return self._risk_view(conn, risk_id)

    def event_impact(self, event_id):
        """事件的完整传播视图：基础风险 + 各落点派生风险。"""
        with self._store.read() as conn:
            ev = self._one(conn, "SELECT * FROM events WHERE event_id=?", (event_id,))
            if ev is None:
                raise NotFound("event_not_found", f"事件不存在: {event_id}")
            now_iso = self._now_iso()
            impacts = []
            for row in self._all(conn, """
                SELECT r.*, rb.decayed_severity, rb.path FROM risk_bases rb
                JOIN risks r ON r.risk_id = rb.risk_id
                WHERE rb.event_id=? ORDER BY r.project_id""", (event_id,)):
                item = dict(row)
                item["path"] = json.loads(item["path"])
                item["effective_status"] = self._effective_status(conn, item, now_iso)
                proj = self._one(conn, "SELECT region FROM projects WHERE project_id=?",
                                 (item["project_id"],))
                item["region"] = proj["region"] if proj else None
                impacts.append(item)
            return {"event": dict(ev),
                    "base_risk": self._risk_view(conn, BASE_PREFIX + event_id, now_iso),
                    "impacts": impacts}

    def project_impact(self, project_id):
        """项目视角：落在该项目上的全部风险及其依据、措施与豁免。"""
        with self._store.read() as conn:
            proj = self._one(conn, "SELECT * FROM projects WHERE project_id=?",
                             (project_id,))
            if proj is None:
                raise NotFound("project_not_found", f"项目不存在: {project_id}")
            now_iso = self._now_iso()
            risks = [self._risk_view(conn, r["risk_id"], now_iso) for r in self._all(
                conn, "SELECT risk_id FROM risks WHERE project_id=? ORDER BY risk_id",
                (project_id,))]
            return {"project": dict(proj), "risks": risks}

    def impact_summary(self):
        """管理视图：各项目风险计数、最高未决级别与最近时限。"""
        with self._store.read() as conn:
            now_iso = self._now_iso()
            projects = {}
            totals = {"open": 0, "exempted": 0, "resolved": 0, "closed": 0}
            for proj in self._all(conn, "SELECT * FROM projects ORDER BY project_id"):
                entry = {"region": proj["region"], "open": 0, "exempted": 0,
                         "resolved": 0, "closed": 0, "max_open_severity": None,
                         "nearest_deadline": None}
                best_rank = 0
                for risk in self._all(conn, "SELECT * FROM risks WHERE project_id=?",
                                      (proj["project_id"],)):
                    eff = self._effective_status(conn, risk, now_iso)
                    entry[eff] += 1
                    totals[eff] += 1
                    if eff == "open":
                        if RANK[risk["severity"]] > best_rank:
                            best_rank = RANK[risk["severity"]]
                            entry["max_open_severity"] = risk["severity"]
                        deadline = risk["deadline"]
                        if deadline and (entry["nearest_deadline"] is None
                                         or deadline < entry["nearest_deadline"]):
                            entry["nearest_deadline"] = deadline
                projects[proj["project_id"]] = entry
            return {"generated_at": now_iso, "projects": projects, "totals": totals}

    def audit_log(self, entity=None, entity_id=None, limit=500):
        sql = "SELECT * FROM audit"
        cond, args = [], []
        if entity:
            cond.append("entity=?")
            args.append(entity)
        if entity_id:
            cond.append("entity_id=?")
            args.append(entity_id)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY seq LIMIT ?"
        args.append(int(limit))
        with self._store.read() as conn:
            return [dict(r) for r in self._all(conn, sql, args)]

    # ---------- 维护：租约到期回收与逾期自动升级 ----------

    def sweep(self):
        """回收过期租约；对逾期未决风险按规则自动升级（幂等，可反复调用）。"""
        with self._store.tx() as conn:
            now_iso = self._now_iso()
            today = self._now().date().isoformat()
            expired = []
            for lease in self._all(conn, "SELECT * FROM leases WHERE expires_at<=?",
                                   (now_iso,)):
                conn.execute("DELETE FROM leases WHERE resource=?", (lease["resource"],))
                self._audit(conn, "system", "lease_expired", "measure",
                            lease["resource"][len(LEASE_PREFIX):],
                            {"previous_owner": lease["owner"]})
                expired.append(dict(lease))
            escalated = []
            for risk in self._all(conn,
                                  "SELECT * FROM risks WHERE status='open'"
                                  " AND deadline IS NOT NULL AND deadline<?", (today,)):
                rules = self._rules(conn, risk["rule_version"])
                if not rules.get("auto_escalate_overdue", True):
                    continue
                target = next_up(risk["severity"])
                if target is None:
                    continue
                esc_id = f"auto:{risk['risk_id']}:{target}"
                if self._one(conn, "SELECT 1 FROM escalations WHERE esc_id=?",
                             (esc_id,)) is not None:
                    continue
                conn.execute(
                    "INSERT INTO escalations(esc_id, risk_id, to_severity, reason, actor,"
                    " created_at) VALUES (?,?,?,?,?,?)",
                    (esc_id, risk["risk_id"], target, "overdue", "system", now_iso))
                self._audit(conn, "system", "risk_escalated", "risk", risk["risk_id"],
                            {"esc_id": esc_id, "to_severity": target,
                             "reason": "overdue"})
                self._refresh_risk(conn, risk["risk_id"])
                escalated.append({"risk_id": risk["risk_id"], "to_severity": target})
            return {"expired_leases": expired, "escalated": escalated}

    # ---------- 测试与排障辅助 ----------

    def dump_state(self):
        """确定性全量状态快照（不含审计流水），用于一致性校验。"""
        tables = ("events", "risks", "risk_bases", "measures", "leases",
                  "exemptions", "escalations", "postmortems", "pending_revocations")
        with self._store.read() as conn:
            return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                    for t in tables}
