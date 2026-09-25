"""合作办学风险联动的服务端包入口。"""
from .app.service import RiskService

PROJECT_CODE = "service_09252_007"

__all__ = ["RiskService", "PROJECT_CODE", "project_info"]


def project_info() -> dict[str, str]:
    """返回稳定的项目标识。"""
    return {"code": PROJECT_CODE, "title": "合作办学风险联动"}
