"""风险严重级别及其排序。"""

SEVERITIES = ("low", "medium", "high", "critical")
RANK = {name: idx + 1 for idx, name in enumerate(SEVERITIES)}


def rank(name):
    """级别名称 -> 1..4 的数值。"""
    return RANK[name]


def at_rank(value):
    """1..4 的数值 -> 级别名称。"""
    return SEVERITIES[value - 1]


def next_up(name):
    """上一档级别；已是最高级时返回 None。"""
    idx = RANK[name]
    return SEVERITIES[idx] if idx < len(SEVERITIES) else None
