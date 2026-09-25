"""SQLite 持久化：模式定义、线程本地连接与事务助手。

WAL 模式下单写多读；写事务一律 BEGIN IMMEDIATE 先取写锁，
配合 leases 表上的部分唯一索引保证同一资源同一时刻至多一个活跃租约。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    message_id  TEXT PRIMARY KEY,          -- 幂等键：重复消息只入库一次
    event_type  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,             -- 业务发生时间，重放排序依据
    received_at TEXT NOT NULL,             -- 服务接收时间，仅作记录
    actor       TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_replay ON events(occurred_at, message_id);

CREATE TABLE IF NOT EXISTS risks (
    risk_key     TEXT PRIMARY KEY,         -- 基础风险为 risk_id，派生风险为 derived:<项目>:<类别>
    kind         TEXT NOT NULL,            -- base | derived
    project_id   TEXT NOT NULL,
    category     TEXT NOT NULL,
    severity     INTEGER NOT NULL,
    status       TEXT NOT NULL,            -- open | closed
    opened_at    TEXT NOT NULL,
    closed_at    TEXT,
    close_reason TEXT,                     -- revoked | basis_empty
    deadline     TEXT,                     -- 处置截止日 YYYY-MM-DD（地区工作日历）
    basis_json   TEXT NOT NULL DEFAULT '[]', -- 派生风险的依据：贡献它的基础风险集合
    rule_version INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_risks_project ON risks(project_id);

CREATE TABLE IF NOT EXISTS exemptions (
    exemption_id TEXT PRIMARY KEY,
    risk_key     TEXT NOT NULL,
    approved_by  TEXT NOT NULL,            -- 人工确认人
    reason       TEXT NOT NULL,
    valid_from   TEXT NOT NULL,            -- 明确有效期
    valid_until  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_exemptions_risk ON exemptions(risk_key);

CREATE TABLE IF NOT EXISTS measures (
    measure_id  TEXT PRIMARY KEY,
    risk_key    TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,             -- proposed | claimed | done
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    assignee    TEXT,                      -- 认领后的责任方
    claimed_at  TEXT,
    done_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_measures_risk ON measures(risk_key);

CREATE TABLE IF NOT EXISTS leases (
    lease_id      TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    owner         TEXT NOT NULL,
    acquired_at   TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    released_at   TEXT,
    status        TEXT NOT NULL            -- active | expired | released
);
-- 并发认领的最终防线：同一资源至多一条 active 租约。
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_lease
    ON leases(resource_type, resource_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS escalations (
    escalation_id TEXT PRIMARY KEY,
    risk_key      TEXT NOT NULL,
    level         INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_escalations_risk ON escalations(risk_key);

CREATE TABLE IF NOT EXISTS reviews (
    review_id    TEXT PRIMARY KEY,
    risk_key     TEXT NOT NULL,
    summary      TEXT NOT NULL,
    root_cause   TEXT NOT NULL DEFAULT '',
    actions_json TEXT NOT NULL DEFAULT '[]',
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_risk ON reviews(risk_key);

CREATE TABLE IF NOT EXISTS audit (
    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit(entity_type, entity_id);
"""


class Database:
    """线程本地连接的 SQLite 访问层。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.connection().executescript(SCHEMA)

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path, timeout=10.0, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self):
        """写事务：立即取写锁，异常回滚。公共服务方法各自持有事务，不嵌套。"""
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.connection().execute(sql, args).fetchall()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
