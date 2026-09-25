"""版本化规则。

传播范围（沿哪些依赖边、衰减多少、最深几层）与处置时限（各级别工作日数）
都由规则集给出。规则按版本注册、不可变；每条风险记录计算时使用的规则版本，
保证口径可回放、可审计。
"""
from ..errors import InvalidInput
from .severity import SEVERITIES

DEFAULT_RULE_VERSION = "v1"

DEFAULT_RULES = {
    "propagation": {
        # 签证问题可沿签证依赖与共用校区关系扩散
        "visa": {"edge_types": ["visa", "campus"], "max_depth": 4, "decay_per_hop": 1},
        # 标准/认证问题沿标准与课程体系扩散
        "standard": {"edge_types": ["standard", "curriculum"], "max_depth": 3, "decay_per_hop": 1},
        # 师资问题沿师资共享与共用校区扩散
        "faculty": {"edge_types": ["faculty", "campus"], "max_depth": 2, "decay_per_hop": 1},
    },
    "deadline_workdays": {"critical": 3, "high": 7, "medium": 15, "low": 30},
    "auto_escalate_overdue": True,
}


def validate_rules(payload):
    """校验并规范化规则载荷，返回可安全持久化的副本。"""
    if not isinstance(payload, dict):
        raise InvalidInput("invalid_rules", "规则必须是对象")
    propagation = payload.get("propagation")
    if not isinstance(propagation, dict) or not propagation:
        raise InvalidInput("invalid_rules", "rules.propagation 必须是非空对象")
    norm_prop = {}
    for category, cfg in propagation.items():
        if not isinstance(category, str) or not category:
            raise InvalidInput("invalid_rules", "风险类别必须是非空字符串")
        if not isinstance(cfg, dict):
            raise InvalidInput("invalid_rules", f"类别 {category} 的配置必须是对象")
        edge_types = cfg.get("edge_types")
        if not isinstance(edge_types, list) or not edge_types or \
                not all(isinstance(t, str) and t for t in edge_types):
            raise InvalidInput("invalid_rules", f"类别 {category} 的 edge_types 必须是非空字符串数组")
        max_depth = cfg.get("max_depth")
        decay = cfg.get("decay_per_hop")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
            raise InvalidInput("invalid_rules", f"类别 {category} 的 max_depth 必须是非负整数")
        if not isinstance(decay, int) or isinstance(decay, bool) or decay < 0:
            raise InvalidInput("invalid_rules", f"类别 {category} 的 decay_per_hop 必须是非负整数")
        norm_prop[category] = {
            "edge_types": sorted(set(edge_types)),
            "max_depth": max_depth,
            "decay_per_hop": decay,
        }
    deadlines = payload.get("deadline_workdays")
    if not isinstance(deadlines, dict):
        raise InvalidInput("invalid_rules", "rules.deadline_workdays 必须是对象")
    norm_deadlines = {}
    for sev in SEVERITIES:
        days = deadlines.get(sev)
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise InvalidInput("invalid_rules", f"deadline_workdays.{sev} 必须是正整数")
        norm_deadlines[sev] = days
    auto = payload.get("auto_escalate_overdue", True)
    if not isinstance(auto, bool):
        raise InvalidInput("invalid_rules", "auto_escalate_overdue 必须是布尔值")
    return {
        "propagation": norm_prop,
        "deadline_workdays": norm_deadlines,
        "auto_escalate_overdue": auto,
    }
