"""跨地区工作日历：周末结构、公共假期与跨地区时限差异。"""
import unittest
from datetime import date

from service_09252_007.domain.calendars import BUILTIN_CALENDARS

import helpers


class WorkdayCalendarTests(unittest.TestCase):
    def setUp(self):
        self.cn = BUILTIN_CALENDARS["CN"]
        self.gulf = BUILTIN_CALENDARS["GULF"]

    def test_weekend_structure_differs_across_regions(self):
        thursday = date(2026, 9, 24)
        # CN 周六日休：周四 +1 个工作日是周五
        self.assertEqual(self.cn.add(thursday, 1), date(2026, 9, 25))
        # GULF 周五六休：周四 +1 个工作日是周日
        self.assertEqual(self.gulf.add(thursday, 1), date(2026, 9, 27))

    def test_public_holidays_are_skipped(self):
        # 2026-10-01..10-07 为 CN 国庆假期
        self.assertEqual(self.cn.add(date(2026, 9, 30), 1), date(2026, 10, 8))
        # GULF 无该假期
        self.assertEqual(self.gulf.add(date(2026, 9, 30), 1), date(2026, 10, 1))

    def test_workdays_between_signed(self):
        self.assertEqual(self.cn.workdays_between(date(2026, 9, 25), date(2026, 9, 30)), 3)
        self.assertEqual(self.cn.workdays_between(date(2026, 9, 30), date(2026, 9, 25)), -3)
        self.assertEqual(self.cn.workdays_between(date(2026, 9, 25), date(2026, 9, 25)), 0)

    def test_multi_week_span(self):
        # 周五起 7 个 CN 工作日：跨过周末与国庆假期
        self.assertEqual(self.cn.add(date(2026, 9, 25), 7), date(2026, 10, 13))


class CrossRegionDeadlineTests(helpers.ServiceTestCase):
    def test_same_event_yields_region_specific_deadlines(self):
        self.svc.create_project("PCN", "中方项目", "CN")
        self.svc.create_project("PGL", "海湾项目", "GULF")
        self.svc.add_dependency("PCN", "PGL", "visa")
        result = self.svc.import_events([
            helpers.make_event("E1", "PCN", "visa", "critical")])[0]
        self.assertEqual(result["status"], "imported")
        # 基础风险：CN 日历，critical 3 个工作日（周五 -> 下周一二三）
        base = self.svc.get_risk("B:E1")
        self.assertEqual(base["deadline"], "2026-09-30")
        # 派生风险：GULF 日历，衰减为 high 7 个工作日（跨周五六周末）
        derived = self.svc.get_risk("D:visa:PGL")
        self.assertEqual(derived["severity"], "high")
        self.assertEqual(derived["deadline"], "2026-10-05")

    def test_custom_calendar_registration(self):
        # 周日单休的地区
        self.svc.register_calendar("SIXDAY", weekend=[6], holidays=["2026-09-28"])
        self.svc.create_project("PX", "六天工作制项目", "SIXDAY")
        self.svc.import_events([helpers.make_event("E2", "PX", "visa", "critical")])
        # 周日休 + 09-28 假期：3 个工作日为 09-26、09-29、09-30
        self.assertEqual(self.svc.get_risk("B:E2")["deadline"], "2026-09-30")


if __name__ == "__main__":
    unittest.main()
