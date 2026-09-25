"""工作日历：周末与地区节假日之外的日期计为工作日。"""
from __future__ import annotations

from datetime import date, timedelta


def is_workday(day: date, holidays: set[date] | frozenset[date] = frozenset()) -> bool:
    """周一到周五且不是地区节假日即为工作日。"""
    return day.weekday() < 5 and day not in holidays


def add_workdays(start: date, count: int, holidays: set[date] | frozenset[date] = frozenset()) -> date:
    """从 start 次日起数 count 个工作日，返回截止日期。

    例：start 为周五、count 为 1 且无节假日时，截止日为下周一。
    """
    if count < 0:
        raise ValueError("count 不能为负")
    day = start
    remaining = count
    while remaining:
        day += timedelta(days=1)
        if is_workday(day, holidays):
            remaining -= 1
    return day
