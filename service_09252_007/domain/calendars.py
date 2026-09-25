"""跨地区工作日历。

不同合作地区周末结构与公共假期不同（如中国大陆周六日休、海湾地区周五六休），
处置时限一律按风险落点项目的地区日历以工作日计算。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class WorkdayCalendar:
    weekend: frozenset  # weekday() 编号，周一=0
    holidays: frozenset  # date 集合

    def is_workday(self, day):
        return day.weekday() not in self.weekend and day not in self.holidays

    def add(self, start, workdays):
        """从 start 起向后数 workdays 个工作日（不含 start 当天）。"""
        if workdays < 0:
            raise ValueError("workdays 不能为负")
        day = start
        remaining = workdays
        while remaining:
            day += timedelta(days=1)
            if self.is_workday(day):
                remaining -= 1
        return day

    def workdays_between(self, a, b):
        """a 到 b 的工作日数（不含 a、含 b）；b 早于 a 时为负。"""
        if a == b:
            return 0
        sign = 1
        lo, hi = a, b
        if hi < lo:
            lo, hi = hi, lo
            sign = -1
        count = 0
        day = lo
        while day < hi:
            day += timedelta(days=1)
            if self.is_workday(day):
                count += 1
        return sign * count


def _dates(*iso):
    return frozenset(date.fromisoformat(s) for s in iso)


BUILTIN_CALENDARS = {
    # 中国大陆：周六日休，含 2026 年主要公共假期（可通过 API 注册更精确的日历覆盖）
    "CN": WorkdayCalendar(
        weekend=frozenset({5, 6}),
        holidays=_dates(
            "2026-01-01",
            "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
            "2026-04-06",
            "2026-05-01",
            "2026-06-19",
            "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
        ),
    ),
    # 海湾地区校区：周五、周六休
    "GULF": WorkdayCalendar(weekend=frozenset({4, 5}), holidays=frozenset()),
    # 欧美校区：周六日休
    "WESTERN": WorkdayCalendar(weekend=frozenset({5, 6}), holidays=frozenset()),
}
