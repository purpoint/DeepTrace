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

    UNAUTHENTICATED = "unauthenticated"
    """No usable credential was presented."""

    TOKEN_EXPIRED = "token_expired"  # noqa: S105 -- an error code, not a credential
    """A credential that was valid and no longer is.

    Split from ``unauthenticated`` because it is the one authentication failure
    a client should handle silently: refresh and retry, rather than show a login
    screen. It reveals nothing -- the expiry is written in the token the client
    already holds."""

    RATE_LIMITED = "rate_limited"


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
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}
        self.headers = headers or {}
        """Response headers this failure requires.

        Some errors are only complete with one: a 401 without
        ``WWW-Authenticate`` does not say what scheme to use, and a 429 without
        ``Retry-After`` leaves the client guessing how long to wait -- which it
        will guess wrong, in the direction that hurts."""

    @classmethod
    def not_found(cls, what: str, identifier: str) -> ApiError:
        return cls(
            ErrorCode.NOT_FOUND,
            f"No {what} with id {identifier}.",
            http_status=status.HTTP_404_NOT_FOUND,
            details={"id": identifier},
        )

    @classmethod
    def unauthenticated(
        cls, message: str = "Sign in to continue.", *, expired: bool = False
    ) -> ApiError:
        """No credential, or one that will not be honoured.

        401, never 403. The two are routinely confused: 401 means "I do not know
        who you are", 403 means "I know, and the answer is still no". Reading
        another user's research is neither -- it is a 404, because a 403 would
        confirm that the id exists.
        """
        return cls(
            ErrorCode.TOKEN_EXPIRED if expired else ErrorCode.UNAUTHENTICATED,
            message,
            http_status=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )

    @classmethod
    def invalid(cls, message: str) -> ApiError:
        """A request this endpoint understood and will not act on.

        422 rather than 400, matching what FastAPI's own validation returns, so
        a client has one status to associate with "the body was wrong" instead
        of two that mean the same thing.
        """
        return cls(
            ErrorCode.INVALID_REQUEST,
            message,
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @classmethod
    def conflict(cls, message: str) -> ApiError:
        return cls(ErrorCode.CONFLICT, message, http_status=status.HTTP_409_CONFLICT)

    @classmethod
    def rate_limited(cls, retry_after: int, *, limit: int) -> ApiError:
        """Too many requests, with the time to wait rather than a scolding."""
        return cls(
            ErrorCode.RATE_LIMITED,
            f"Too many requests. Try again in {retry_after} seconds.",
            http_status=status.HTTP_429_TOO_MANY_REQUESTS,
            details={"retry_after": retry_after, "limit": limit},
            headers={"Retry-After": str(retry_after)},
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


def _render(
    error: ErrorBody, http_status: int, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=ErrorResponse(error=error).model_dump(mode="json"),
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Route every kind of failure into the one envelope."""

    @app.exception_handler(ApiError)
    async def _handled(_request: Request, exc: ApiError) -> JSONResponse:
        return _render(
            ErrorBody(code=exc.code, message=exc.message, details=exc.details),
            exc.http_status,
            exc.headers,
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
