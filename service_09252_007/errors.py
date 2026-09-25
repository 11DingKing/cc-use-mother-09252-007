"""服务层异常类型，API 边界据此映射 HTTP 状态码。"""
from __future__ import annotations


class RiskServiceError(Exception):
    """服务层错误基类。"""

    status = 500
    code = "internal_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def payload(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class ValidationError(RiskServiceError):
    """输入校验失败。"""

    status = 400
    code = "validation_error"


class NotFoundError(RiskServiceError):
    """目标资源不存在。"""

    status = 404
    code = "not_found"


class ConflictError(RiskServiceError):
    """状态冲突（如措施已被他人认领）。"""

    status = 409
    code = "conflict"
