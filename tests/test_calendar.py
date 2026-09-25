"""工作日历与跨地区处置时限。"""
from __future__ import annotations

import unittest
from datetime import date

import helpers
from service_09252_007.calendar import add_workdays, is_workday
from service_09252_007.projection import derived_key

# 2026 年国庆假期：10 月 1 日（周四）至 10 月 7 日（周三）
CN_GOLDEN_WEEK = {date(2026, 10, d) for d in range(1, 8)}


class WorkdayCalendarTests(helpers.ServiceTestCase):
    def test_weekend_and_holiday_skipping(self):
        # 2026-09-30 是周三；CN 跳过 10/1-10/7 与周末后为 10/8、10/9
        self.assertEqual(add_workdays(date(2026, 9, 30), 2, CN_GOLDEN_WEEK), date(2026, 10, 9))
        # 无节假日地区：10/1（周四）、10/2（周五）
        self.assertEqual(add_workdays(date(2026, 9, 30), 2, frozenset()), date(2026, 10, 2))
        self.assertFalse(is_workday(date(2026, 10, 3), frozenset()))  # 周六
        self.assertFalse(is_workday(date(2026, 10, 5), CN_GOLDEN_WEEK))  # 周一但节假日
        self.assertTrue(is_workday(date(2026, 10, 8), CN_GOLDEN_WEEK))

    def test_cross_region_deadlines(self):
        """同一风险在不同地区项目上的处置时限按各自工作日历计算。"""
        self.add_project("CN-1", region="CN")
        self.add_project("UK-1", region="UK")
        for d in range(1, 8):
            self.service.register_holiday("CN", f"2026-10-0{d}")
        self.add_dependency("D1", "CN-1", "UK-1")
        self.raise_risk("R1", "CN-1", severity=5, occurred_at="2026-09-30T09:00:00+00:00")

        base = self.service.get_risk("R1")
        # CN：严重度 5 → 2 个工作日，跳过黄金周 → 10/9
        self.assertEqual(base["deadline"], "2026-10-09")

        derived = self.service.get_risk(derived_key("UK-1", "visa"))
        # UK：传播后严重度 4 → 3 个工作日，无节假日 → 10/1、10/2、10/5
        self.assertEqual(derived["severity"], 4)
        self.assertEqual(derived["deadline"], "2026-10-05")

    def test_holiday_registered_later_recomputes_deadline(self):
        """节假日登记是事件，补登后重建会重算截止日。"""
        self.add_project("CN-1", region="CN")
        self.raise_risk("R1", "CN-1", severity=5, occurred_at="2026-09-30T09:00:00+00:00")
        self.assertEqual(self.service.get_risk("R1")["deadline"], "2026-10-02")

        for d in range(1, 8):
            self.service.register_holiday("CN", f"2026-10-0{d}")
        self.assertEqual(self.service.get_risk("R1")["deadline"], "2026-10-09")


if __name__ == "__main__":
    unittest.main()
