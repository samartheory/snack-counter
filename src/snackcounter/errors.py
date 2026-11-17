class DomainError(Exception):
    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, **context) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.context}


class NotFound(DomainError):
    code = "not_found"
    http_status = 404


class Conflict(DomainError):
    code = "conflict"
    http_status = 409


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class HrUnavailable(Exception):
    """The HR system is down or slow. The outbox keeps the work for a retry."""
