"""SQLite 持久化：模式定义、连接与事务。

所有运行状态（含租约与审计）都落在 SQLite，进程重启后完整恢复。
写事务统一使用 BEGIN IMMEDIATE 取得写锁，配合 busy_timeout 串行化并发写。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendars (
  region   TEXT PRIMARY KEY,
  weekend  TEXT NOT NULL,          -- JSON 数组，weekday() 编号
  holidays TEXT NOT NULL           -- JSON 数组，YYYY-MM-DD
);
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  region     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dependencies (
  dep_id     TEXT PRIMARY KEY,
  src        TEXT NOT NULL,        -- 风险源项目
  dst        TEXT NOT NULL,        -- 受影响（依赖方）项目
  dep_type   TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (src, dst, dep_type)
);
CREATE TABLE IF NOT EXISTS rule_sets (
  version    TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,        -- JSON
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id      TEXT PRIMARY KEY,  -- 客户端幂等键
  project_id    TEXT NOT NULL,
  category      TEXT NOT NULL,
  severity      TEXT NOT NULL,
  occurred_at   TEXT NOT NULL,
  note          TEXT,
  payload_hash  TEXT NOT NULL,     -- 同 id 不同载荷 -> 冲突
  revoked       INTEGER NOT NULL DEFAULT 0,
  revoked_at    TEXT,
  revoke_reason TEXT,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_revocations (
  event_id   TEXT PRIMARY KEY,     -- 乱序到达：撤销先于事件
  reason     TEXT,
  revoked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risks (
  risk_id       TEXT PRIMARY KEY,  -- 基础风险 B:<event_id>；派生风险 D:<category>:<project_id>
  kind          TEXT NOT NULL,     -- base | derived
  project_id    TEXT NOT NULL,
  category      TEXT NOT NULL,
  base_severity TEXT NOT NULL,     -- 传播/事件给出的原始级别
  severity      TEXT NOT NULL,     -- 含升级后的生效级别
  status        TEXT NOT NULL,     -- open | resolved | closed
  close_reason  TEXT,
  deadline      TEXT,              -- YYYY-MM-DD，按落点地区工作日历
  rule_version  TEXT NOT NULL,     -- 计算时使用的规则版本
  created_at    TEXT NOT NULL,
  closed_at     TEXT
);
CREATE TABLE IF NOT EXISTS risk_bases (
  risk_id          TEXT NOT NULL,  -- 派生风险
  event_id         TEXT NOT NULL,  -- 作为依据的源事件
  decayed_severity TEXT NOT NULL,  -- 该路径衰减后的级别
  path             TEXT NOT NULL,  -- JSON 数组，传播路径上的项目
  PRIMARY KEY (risk_id, event_id)
);
CREATE TABLE IF NOT EXISTS escalations (
  esc_id      TEXT PRIMARY KEY,    -- 客户端幂等键
  risk_id     TEXT NOT NULL,
  to_severity TEXT NOT NULL,
  reason      TEXT,
  actor       TEXT,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measures (
  measure_id   TEXT PRIMARY KEY,
  risk_id      TEXT NOT NULL,
  title        TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open',  -- open | done | cancelled
  created_by   TEXT,
  created_at   TEXT NOT NULL,
  completed_at TEXT,
  completed_by TEXT
);
CREATE TABLE IF NOT EXISTS leases (
  resource    TEXT PRIMARY KEY,    -- measure:<measure_id>
  owner       TEXT NOT NULL,
  token       TEXT NOT NULL,       -- 认领幂等键 / 操作凭证
  acquired_at TEXT NOT NULL,
  expires_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exemptions (
  exemption_id TEXT PRIMARY KEY,
  risk_id      TEXT,               -- 精确豁免某条风险
  project_id   TEXT,               -- 或按 项目+类别 豁免
  category     TEXT,
  reason       TEXT NOT NULL,
  confirmed_by TEXT NOT NULL,      -- 人工确认人（必填）
  valid_from   TEXT NOT NULL,      -- 明确有效期
  valid_until  TEXT NOT NULL,
  revoked      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postmortems (
  pm_id      TEXT PRIMARY KEY,
  risk_id    TEXT NOT NULL UNIQUE, -- 每条风险一份复盘
  root_cause TEXT,
  timeline   TEXT,
  lessons    TEXT,
  created_by TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  actor     TEXT NOT NULL,
  action    TEXT NOT NULL,
  entity    TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  detail    TEXT
);
"""


def init_schema(conn):
    conn.executescript(SCHEMA)


class Store:
    """连接与事务管理。`:memory:` 模式使用共享连接，文件模式按操作开连接。"""

    def __init__(self, path):
        self.path = path
        self._shared = None
        self._shared_lock = threading.RLock()
        if path == ":memory:":
            self._shared = self._connect()
            init_schema(self._shared)
        else:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                init_schema(conn)
            finally:
                conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    @contextmanager
    def tx(self):
        """写事务：BEGIN IMMEDIATE，提交或回滚。"""
        if self._shared is not None:
            with self._shared_lock:
                self._shared.execute("BEGIN IMMEDIATE")
                try:
                    yield self._shared
                    self._shared.execute("COMMIT")
                except Exception:
                    self._shared.execute("ROLLBACK")
                    raise
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        """只读连接。"""
        if self._shared is not None:
            with self._shared_lock:
                yield self._shared
            return
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()
