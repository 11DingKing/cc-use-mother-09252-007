"""版本化规则：决定风险传播范围与处置时限。

每条基础风险按其发生时刻生效的规则版本计算传播深度、严重度衰减和
处置工作日数；规则通过 rule_published 事件发布，历史风险不受新规则影响。
"""
from __future__ import annotations

from .errors import ValidationError
from .timeutil import fmt

# 内置兜底规则（版本 0），在未发布任何规则或风险早于全部规则时生效。
BUILTIN_RULE: dict = {
    "version": 0,
    "effective_from": "1970-01-01T00:00:00.000000+00:00",
    "propagating_dependency_kinds": [
        "shared-curriculum",
        "shared-faculty",
        "shared-accreditation",
        "shared-service",
    ],
    "max_depth": 3,
    "severity_decay_per_hop": 1,
    "min_propagated_severity": 1,
    "deadline_workdays": {"5": 2, "4": 3, "3": 5, "2": 8, "1": 13},
}

_RULE_KEYS = (
    "propagating_dependency_kinds",
    "max_depth",
    "severity_decay_per_hop",
    "min_propagated_severity",
    "deadline_workdays",
)


def _is_int(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def normalize_rule(payload: dict) -> dict:
    """校验并规范化一条规则定义，返回可存入事件负载的字典。

    未提供的字段回退到内置规则取值，便于只覆盖部分参数。
    """
    if not isinstance(payload, dict):
        raise ValidationError("规则必须是 JSON 对象")
    version = payload.get("version")
    if not _is_int(version, 1, 1_000_000):
        raise ValidationError("规则 version 必须是 >= 1 的整数")
    effective_raw = payload.get("effective_from")
    if not isinstance(effective_raw, str) or not effective_raw.strip():
        raise ValidationError("规则缺少 effective_from")
    try:
        effective_from = fmt(effective_raw)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"规则 effective_from 非法: {exc}") from exc

    merged = {key: payload.get(key, BUILTIN_RULE[key]) for key in _RULE_KEYS}

    kinds = merged["propagating_dependency_kinds"]
    if not isinstance(kinds, list) or not kinds or not all(
        isinstance(k, str) and k.strip() for k in kinds
    ):
        raise ValidationError("propagating_dependency_kinds 必须是非空字符串列表")
    if not _is_int(merged["max_depth"], 0, 10):
        raise ValidationError("max_depth 必须是 0..10 的整数")
    if not _is_int(merged["severity_decay_per_hop"], 0, 4):
        raise ValidationError("severity_decay_per_hop 必须是 0..4 的整数")
    if not _is_int(merged["min_propagated_severity"], 1, 5):
        raise ValidationError("min_propagated_severity 必须是 1..5 的整数")

    workdays = merged["deadline_workdays"]
    if not isinstance(workdays, dict) or not workdays:
        raise ValidationError("deadline_workdays 必须是非空对象")
    normalized_workdays: dict[str, int] = {}
    for key, value in workdays.items():
        try:
            severity = int(key)
        except (TypeError, ValueError):
            raise ValidationError(f"deadline_workdays 的键非法: {key!r}") from None
        if not 1 <= severity <= 5 or not _is_int(value, 0, 60):
            raise ValidationError("deadline_workdays 键为 1..5，值为 0..60 的整数")
        normalized_workdays[str(severity)] = value

    return {
        "version": version,
        "effective_from": effective_from,
        "propagating_dependency_kinds": sorted({k.strip() for k in kinds}),
        "max_depth": merged["max_depth"],
        "severity_decay_per_hop": merged["severity_decay_per_hop"],
        "min_propagated_severity": merged["min_propagated_severity"],
        "deadline_workdays": normalized_workdays,
    }


def select_rule(rules: list[dict], at: str) -> dict:
    """选取在时刻 at 生效的规则版本；无匹配时回退到内置规则。"""
    best = BUILTIN_RULE
    for rule in rules:
        if rule["effective_from"] <= at and (rule["effective_from"], rule["version"]) >= (
            best["effective_from"],
            best["version"],
        ):
            best = rule
    return best


def deadline_workdays(rule: dict, severity: int) -> int:
    """某严重度对应的处置工作日数，缺档时取 5。"""
    return int(rule["deadline_workdays"].get(str(severity), 5))
