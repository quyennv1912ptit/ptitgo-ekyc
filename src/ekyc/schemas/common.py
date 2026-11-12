from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ekyc.core.errors import EkycError, ErrorCode, WarningCode

T = TypeVar("T")


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Warning(CamelModel):
    code: WarningCode
    field: str | None = None
    message: str | None = None


class ApiResponse(CamelModel, Generic[T]):
    success: bool
    data: T | None = None
    error_code: ErrorCode | None = None
    message: str | None = None
    retryable: bool | None = None
    details: list[dict] | None = None
    warnings: list[Warning] = Field(default_factory=list)
    request_id: str

    @classmethod
    def ok(
        cls, data: T, request_id: str, warnings: list[Warning] | None = None
    ) -> "ApiResponse[T]":
        return cls(success=True, data=data, request_id=request_id, warnings=warnings or [])

    @classmethod
    def fail(cls, error: EkycError, request_id: str) -> "ApiResponse[T]":
        return cls(
            success=False,
            error_code=error.code,
            message=error.message,
            retryable=error.retryable,
            details=error.details,
            request_id=request_id,
        )
