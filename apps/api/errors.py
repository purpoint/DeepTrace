"""One error shape, and one place that decides it.

A client integrating against this API has to handle failure, and handling it is
only possible if failures look the same. Two endpoints returning ``{"detail":
...}`` and ``{"error": ...}`` for the same class of problem means the client
writes two handlers and gets the third one wrong.

So every error leaves through here, in one envelope, with a machine-readable
code beside the human sentence. The code is what a client branches on; the
message is what a person reads. Branching on message text is what happens when
a code is not provided, and it breaks the first time the wording improves.

Nothing internal escapes. An unhandled exception becomes a 500 with a reference
id and nothing else -- the traceback goes to the logs, where it belongs, because
a stack trace in a response body tells an attacker the file layout, the library
versions, and often a query.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.config import MissingConfigurationError
from core.logging import get_logger
from core.observability.recorder import new_run_id

log = get_logger(__name__)


class ErrorCode(StrEnum):
    """What went wrong, in a form a client can branch on.

    Deliberately coarse. A code per internal failure would make the API's error
    surface a mirror of its implementation, and every refactor a breaking
    change for clients.
    """

    NOT_FOUND = "not_found"
    INVALID_REQUEST = "invalid_request"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


class ApiError(Exception):
    """A failure the API knows how to describe.

    Raised by routes instead of ``HTTPException`` so the status code and the
    envelope are decided in one place rather than at each raise site, where they
    drift.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}

    @classmethod
    def not_found(cls, what: str, identifier: str) -> ApiError:
        return cls(
            ErrorCode.NOT_FOUND,
            f"No {what} with id {identifier}.",
            http_status=status.HTTP_404_NOT_FOUND,
            details={"id": identifier},
        )

    @classmethod
    def unavailable(cls, what: str) -> ApiError:
        """A dependency this request needed is not reachable.

        Separated from a 500 because it means something different to a client:
        a 503 is worth retrying, and an internal error is not.
        """
        return cls(
            ErrorCode.UNAVAILABLE,
            f"{what} is not available right now.",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class ErrorBody(BaseModel):
    """The envelope. Every failure response is exactly this."""

    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    reference: str | None = Field(
        default=None,
        description="Present on internal errors: the id this failure was logged under.",
    )


class ErrorResponse(BaseModel):
    error: ErrorBody


def _render(error: ErrorBody, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=ErrorResponse(error=error).model_dump(mode="json"),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Route every kind of failure into the one envelope."""

    @app.exception_handler(ApiError)
    async def _handled(_request: Request, exc: ApiError) -> JSONResponse:
        return _render(
            ErrorBody(code=exc.code, message=exc.message, details=exc.details),
            exc.http_status,
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Validation failures, in the same envelope as everything else.

        FastAPI's own format is a bare list under ``detail``, which is a second
        error shape for clients to learn. The field errors are kept -- they are
        the useful part -- but moved inside the envelope.
        """
        return _render(
            ErrorBody(
                code=ErrorCode.INVALID_REQUEST,
                message="The request body or parameters are not valid.",
                details={"fields": exc.errors()},
            ),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.exception_handler(MissingConfigurationError)
    async def _misconfigured(_request: Request, exc: MissingConfigurationError) -> JSONResponse:
        """A missing credential is the operator's problem, not the caller's.

        Reported as unavailable rather than as a bad request, and without naming
        the setting: which variable is missing is a fact about the deployment,
        and the caller can do nothing with it.
        """
        log.error("api.misconfigured", error=str(exc))
        return _render(
            ErrorBody(
                code=ErrorCode.UNAVAILABLE,
                message="The service is not fully configured.",
            ),
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Anything unforeseen: logged in full, reported as nothing.

        The reference id is the whole contract. It lets a user report a failure
        and an operator find the traceback, without the response carrying the
        file layout, the library versions, or the query that failed.
        """
        reference = new_run_id("err")
        log.error(
            "api.unhandled",
            reference=reference,
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            error=str(exc),
            exc_info=exc,
        )
        return _render(
            ErrorBody(
                code=ErrorCode.INTERNAL,
                message="Something went wrong handling this request.",
                reference=reference,
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
