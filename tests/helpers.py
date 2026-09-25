"""测试公共工具：可注入时钟、临时文件库、标准依赖图。"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from service_09252_007 import RiskService

# 2026-09-25 是周五
FIXED_NOW = datetime(2026, 9, 25, 9, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start=FIXED_NOW):
        self._now = start

    def __call__(self):
        return self._now

    def advance(self, **kwargs):
        self._now += timedelta(**kwargs)


def make_event(event_id, project_id, category="visa", severity="high",
               occurred_at="2026-09-25T08:00:00+00:00"):
    return {"event_id": event_id, "project_id": project_id, "category": category,
            "severity": severity, "occurred_at": occurred_at}


def build_graph(svc):
    """A(CN) -> B(GULF) -> C(CN) -> A 构成环；A->D、B->D 构成菱形。D 在 CN。"""
    svc.create_project("A", "阿尔法项目", "CN")
    svc.create_project("B", "贝塔项目", "GULF")
    svc.create_project("C", "伽马项目", "CN")
    svc.create_project("D", "德尔塔项目", "CN")
    svc.add_dependency("A", "B", "visa")
    svc.add_dependency("B", "C", "visa")
    svc.add_dependency("C", "A", "visa")
    svc.add_dependency("A", "D", "visa")
    svc.add_dependency("B", "D", "visa")


class ServiceTestCase(unittest.TestCase):
    """每个用例一个临时目录文件库 + 固定时钟。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.svc = self.new_service()

    def new_service(self):
        """在同一数据库上重建服务实例（模拟重启）。"""
        return RiskService(os.path.join(self.tmp.name, "risk.db"), clock=self.clock)

    def fresh_service(self):
        """全新独立数据库的服务实例（同一时钟）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return RiskService(os.path.join(tmp.name, "risk.db"), clock=self.clock)
