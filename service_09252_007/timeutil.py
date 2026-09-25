"""时间处理：统一 UTC、固定精度，保证字符串可比较、可按字典序排序。"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """默认时间源；服务中可通过 clock 参数替换。"""
    return datetime.now(timezone.utc)


def to_utc(value: datetime | str) -> datetime:
    """把 datetime 或 ISO 字符串规范化为带时区的 UTC 时间。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"非法时间格式: {value!r}") from exc
    else:
        raise TypeError(f"不支持的时间类型: {type(value)!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt(value: datetime | str) -> str:
    """格式化为固定微秒精度的 UTC ISO 字符串。

    所有持久化时间统一走这里，保证 SQLite 中可按字典序比较与排序。
    """
    return to_utc(value).isoformat(timespec="microseconds")
