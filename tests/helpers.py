"""测试公共工具：假时钟与服务基座。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from service_09252_007.service import RiskService

T0 = "2026-09-25T09:00:00+00:00"  # 周五


class FakeClock:
    """可手动推进的时间源。"""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 9, 25, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


class ServiceTestCase(unittest.TestCase):
    """每个用例一个独立的临时库与可替换时钟。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "risk.db"
        self.clock = FakeClock()
        self.service = self.make_service()
        self.addCleanup(self.service.close)

    def make_service(self) -> RiskService:
        return RiskService(self.db_path, clock=self.clock)

    # 便捷操作 -----------------------------------------------------------
    def add_project(self, project_id: str, region: str = "default", owner: str = ""):
        return self.service.register_project(
            project_id, f"项目{project_id}", region, owner=owner,
            occurred_at="2026-09-01T00:00:00+00:00",
        )

    def add_dependency(self, dep_id: str, upstream: str, downstream: str, kind: str = "shared-faculty"):
        return self.service.add_dependency(
            dep_id, upstream, downstream, kind, occurred_at="2026-09-01T00:00:00+00:00"
        )

    def raise_risk(self, risk_id: str, project_id: str, category: str = "visa",
                   severity: int = 5, occurred_at: str = T0):
        return self.service.raise_risk(risk_id, project_id, category, severity,
                                       occurred_at=occurred_at)
