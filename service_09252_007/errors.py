"""领域错误：携带稳定错误码，接口层据此映射 HTTP 状态。"""


class DomainError(Exception):
    """所有领域错误的基类。"""

    http_status = 400

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


class InvalidInput(DomainError):
    """输入非法（400）。"""

    http_status = 400


class NotFound(DomainError):
    """目标不存在（404）。"""

    http_status = 404


class Conflict(DomainError):
    """与现有状态冲突，如重复标识、租约被持有（409）。"""

    http_status = 409


class InvalidState(DomainError):
    """当前状态不允许该操作（422）。"""

    http_status = 422
