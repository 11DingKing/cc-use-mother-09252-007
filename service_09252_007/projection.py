"""投影重建：风险状态是事件日志的纯函数。

所有事件按 (occurred_at, message_id) 排序重放，与到达顺序无关，
因此乱序投递与重复投递产生一致的结果。重建写入的时间戳取自事件
本身（as_of = 最新事件的 occurred_at），同一事件流在任何时刻重放
都得到逐字节一致的风险表与审计流。

派生风险按 (项目, 类别) 聚合，其 basis 是所有能传播到该项目的基础
风险集合；基础风险被撤销后重放，只有 basis 为空的派生风险才被关闭。
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import date

from .calendar import add_workdays
from .rules import deadline_workdays, select_rule
from .timeutil import to_utc

DERIVED_PREFIX = "derived:"

# 重建时逐字段比对并同步的列
_TRACKED_FIELDS = (
    "kind",
    "project_id",
    "category",
    "severity",
    "status",
    "opened_at",
    "closed_at",
    "close_reason",
    "deadline",
    "rule_version",
)


def derived_key(project_id: str, category: str) -> str:
    return f"{DERIVED_PREFIX}{project_id}:{category}"


def rebuild(conn) -> dict:
    """根据 events 表全量重建 risks 表，返回变更摘要。"""
    rows = conn.execute(
        "SELECT message_id, event_type, occurred_at, payload FROM events "
        "ORDER BY occurred_at, message_id"
    ).fetchall()
    if not rows:
        return {"opened": [], "closed": [], "reopened": [], "updated": []}
    as_of = rows[-1]["occurred_at"]  # 重放序最后一条事件的发生时间

    projects: dict[str, dict] = {}
    deps: dict[str, dict] = {}
    holidays: dict[str, set[date]] = defaultdict(set)
    rules: dict[int, dict] = {}
    bases: dict[str, dict] = {}

    for row in rows:
        payload = json.loads(row["payload"])
        etype = row["event_type"]
        if etype == "project_registered":
            projects[payload["project_id"]] = {
                "name": payload["name"],
                "region": payload["region"],
                "owner": payload.get("owner", ""),
            }
        elif etype == "dependency_added":
            deps[payload["dependency_id"]] = payload
        elif etype == "dependency_removed":
            deps.pop(payload["dependency_id"], None)
        elif etype == "holiday_registered":
            holidays[payload["region"]].add(date.fromisoformat(payload["day"]))
        elif etype == "rule_published":
            rules[payload["version"]] = payload  # 同版本重复发布：重放序后者生效
        elif etype == "risk_raised":
            rid = payload["risk_id"]
            if rid not in bases:  # 同一 risk_id 重复上报：重放序先到者生效
                bases[rid] = {
                    "risk_id": rid,
                    "project_id": payload["project_id"],
                    "category": payload["category"],
                    "severity": int(payload["severity"]),
                    "opened_at": row["occurred_at"],
                    "revoked_at": None,
                    "revoke_reason": "",
                }
        elif etype == "risk_revoked":
            base = bases.get(payload["risk_id"])
            if base is not None and base["revoked_at"] is None:
                base["revoked_at"] = row["occurred_at"]
                base["revoke_reason"] = payload.get("reason", "")

    rule_list = sorted(rules.values(), key=lambda r: (r["effective_from"], r["version"]))

    desired: dict[str, dict] = {}
    active: list[tuple[dict, dict]] = []
    for base in bases.values():
        rule = select_rule(rule_list, base["opened_at"])
        region = projects.get(base["project_id"], {}).get("region", "default")
        opened_date = to_utc(base["opened_at"]).date()
        deadline = add_workdays(
            opened_date,
            deadline_workdays(rule, base["severity"]),
            holidays.get(region, frozenset()),
        )
        revoked = base["revoked_at"] is not None
        desired[base["risk_id"]] = {
            "risk_key": base["risk_id"],
            "kind": "base",
            "project_id": base["project_id"],
            "category": base["category"],
            "severity": base["severity"],
            "status": "closed" if revoked else "open",
            "opened_at": base["opened_at"],
            "closed_at": base["revoked_at"],
            "close_reason": "revoked" if revoked else None,
            "deadline": deadline.isoformat(),
            "basis": [],
            "rule_version": rule["version"],
        }
        if not revoked:
            active.append((base, rule))

    # 传播：以每个未撤销的基础风险为源点按层 BFS；visited 集合保证依赖环收敛。
    contributions: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for base, rule in active:
        kinds = set(rule["propagating_dependency_kinds"])
        adjacency: dict[str, list[str]] = defaultdict(list)
        for dep in deps.values():
            if dep["kind"] in kinds:
                adjacency[dep["upstream"]].append(dep["downstream"])
        decay = rule["severity_decay_per_hop"]
        visited = {base["project_id"]}
        queue = deque([(base["project_id"], 0)])
        while queue:
            node, hops = queue.popleft()
            if hops >= rule["max_depth"]:
                continue
            for nxt in sorted(adjacency.get(node, ())):
                severity = base["severity"] - decay * (hops + 1)
                if nxt in visited or severity < rule["min_propagated_severity"]:
                    continue
                visited.add(nxt)
                contributions[(nxt, base["category"])].append(
                    {
                        "base_risk_id": base["risk_id"],
                        "severity": severity,
                        "hops": hops + 1,
                        "opened_at": base["opened_at"],
                        "rule": rule,
                    }
                )
                queue.append((nxt, hops + 1))

    for (project_id, category), items in contributions.items():
        region = projects.get(project_id, {}).get("region", "default")
        hols = holidays.get(region, frozenset())
        # 处置时限取各贡献源中最紧迫者：发生日 + 按贡献严重度折算的工作日数。
        deadlines = [
            add_workdays(
                to_utc(item["opened_at"]).date(),
                deadline_workdays(item["rule"], item["severity"]),
                hols,
            )
            for item in items
        ]
        primary = sorted(items, key=lambda i: (-i["severity"], i["base_risk_id"]))[0]
        basis = sorted(
            (
                {
                    "base_risk_id": item["base_risk_id"],
                    "severity": item["severity"],
                    "hops": item["hops"],
                    "opened_at": item["opened_at"],
                }
                for item in items
            ),
            key=lambda i: i["base_risk_id"],
        )
        key = derived_key(project_id, category)
        desired[key] = {
            "risk_key": key,
            "kind": "derived",
            "project_id": project_id,
            "category": category,
            "severity": max(item["severity"] for item in items),
            "status": "open",
            "opened_at": min(item["opened_at"] for item in items),
            "closed_at": None,
            "close_reason": None,
            "deadline": min(deadlines).isoformat(),
            "basis": basis,
            "rule_version": primary["rule"]["version"],
        }

    return _apply(conn, desired, as_of)


def _apply(conn, desired: dict[str, dict], as_of: str) -> dict:
    """把期望状态与 risks 表比对，只对有变化的行写入并审计。"""
    stored = {
        row["risk_key"]: dict(row) for row in conn.execute("SELECT * FROM risks").fetchall()
    }
    summary = {"opened": [], "closed": [], "reopened": [], "updated": []}

    for key in sorted(desired):
        target = desired[key]
        current = stored.pop(key, None)
        if current is None:
            conn.execute(
                "INSERT INTO risks(risk_key, kind, project_id, category, severity, status,"
                " opened_at, closed_at, close_reason, deadline, basis_json, rule_version,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    target["risk_key"],
                    target["kind"],
                    target["project_id"],
                    target["category"],
                    target["severity"],
                    target["status"],
                    target["opened_at"],
                    target["closed_at"],
                    target["close_reason"],
                    target["deadline"],
                    json.dumps(target["basis"], sort_keys=True),
                    target["rule_version"],
                    as_of,
                ),
            )
            _audit(
                conn,
                as_of,
                "risk_opened",
                key,
                {
                    "kind": target["kind"],
                    "project_id": target["project_id"],
                    "category": target["category"],
                    "severity": target["severity"],
                    "deadline": target["deadline"],
                    "basis": target["basis"],
                    "rule_version": target["rule_version"],
                },
            )
            summary["opened"].append(key)
            continue

        changes = {}
        for field in _TRACKED_FIELDS:
            if current[field] != target[field]:
                changes[field] = {"from": current[field], "to": target[field]}
        if json.loads(current["basis_json"]) != target["basis"]:
            changes["basis"] = {
                "from": json.loads(current["basis_json"]),
                "to": target["basis"],
            }
        if not changes:
            continue
        conn.execute(
            "UPDATE risks SET kind=?, project_id=?, category=?, severity=?, status=?,"
            " opened_at=?, closed_at=?, close_reason=?, deadline=?, basis_json=?,"
            " rule_version=?, updated_at=? WHERE risk_key=?",
            (
                target["kind"],
                target["project_id"],
                target["category"],
                target["severity"],
                target["status"],
                target["opened_at"],
                target["closed_at"],
                target["close_reason"],
                target["deadline"],
                json.dumps(target["basis"], sort_keys=True),
                target["rule_version"],
                as_of,
                key,
            ),
        )
        if current["status"] == "closed" and target["status"] == "open":
            action = "risk_reopened"
            summary["reopened"].append(key)
        elif current["status"] == "open" and target["status"] == "closed":
            action = "risk_closed"
            summary["closed"].append(key)
        else:
            action = "risk_updated"
            summary["updated"].append(key)
        _audit(conn, as_of, action, key, {"changes": changes})

    # 期望集合中消失的派生风险：依据已空，关闭之（保留行供审计追溯）。
    for key, current in sorted(stored.items()):
        if current["status"] == "closed" and current["close_reason"] == "basis_empty":
            continue  # 已是空依据关闭态，避免重复审计
        conn.execute(
            "UPDATE risks SET status='closed', closed_at=?, close_reason='basis_empty',"
            " basis_json='[]', updated_at=? WHERE risk_key=?",
            (as_of, as_of, key),
        )
        _audit(
            conn,
            as_of,
            "risk_closed",
            key,
            {
                "reason": "basis_empty",
                "previous_basis": json.loads(current["basis_json"]),
            },
        )
        summary["closed"].append(key)

    return summary


def _audit(conn, as_of: str, action: str, risk_key: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit(ts, actor, action, entity_type, entity_id, detail)"
        " VALUES (?,?,?,?,?,?)",
        (as_of, "projection", action, "risk", risk_key, json.dumps(detail, sort_keys=True)),
    )
