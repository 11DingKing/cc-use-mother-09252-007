"""合作办学风险联动的服务端包入口。"""
from __future__ import annotations

PROJECT_CODE = "service_09252_007"


def project_info() -> dict[str, str]:
    """返回稳定的项目标识。"""
    return {"code": PROJECT_CODE, "title": "合作办学风险联动"}


# 包级便捷导出（置于末尾，避免子模块导入时循环依赖）。
from .service import RiskService  # noqa: E402

__all__ = ["PROJECT_CODE", "project_info", "RiskService"]
