"""Who is making this request, and what they are allowed to see.

Two dependencies, and the second is the interesting one.

``current_user`` reads the bearer token, verifies it, and loads the account. It
is ordinary.

``get_repository`` builds the research repository *scoped to that account*, and
that is the design: there is no way to obtain a repository in a request without
having authenticated, because the only provider for one takes the current user
as an argument. A route asks for ``Repository`` and receives an object that can
only see its caller's research. Forgetting the ownership check is not a mistake
that can be made here, because there is no check to forget -- it is in the
queries the repository issues.

The alternative, which this replaces, was a repository that saw everything and
routes that remembered to compare ids. That works until the tenth route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.providers import get_session, get_settings_from
from apps.api.errors import ApiError
from core.config import Settings
from core.logging import bind_research_context, get_logger
from infrastructure.auth.tokens import InvalidToken, TokenKind, read
from infrastructure.db.repositories.research import ResearchRepository
from infrastructure.db.repositories.scope import Viewer
from infrastructure.db.repositories.users import UserRepository

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated account, in the shape a route actually needs.

    Not the SQLAlchemy row. Handing routes the ORM object would put a password
    hash one attribute access away from a response model, and the first time
    someone returns ``user`` directly from an endpoint it would go out on the
    wire. What a route needs is an id, an address, and a name.
    """

    id: str
    email: str
    display_name: str | None

    @property
    def viewer(self) -> Viewer:
        return Viewer.user(self.id)


def bearer_token(request: Request) -> str:
    """Pull the credential out of the Authorization header.

    The scheme is compared case-insensitively because RFC 7235 says it is
    case-insensitive, and clients that send ``bearer`` are not wrong.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError.unauthenticated()
    return token.strip()


async def current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings_from)],
) -> CurrentUser:
    """Verify the bearer token and load the account it names.

    The database lookup is what makes deactivation take effect. Trusting the
    token alone would be faster and would mean a disabled account keeps working
    until its access token expires -- which is a strange thing to tell someone
    who just reported their laptop stolen.
    """
    token = bearer_token(request)

    try:
        claims = read(token, TokenKind.ACCESS, settings)
    except InvalidToken as exc:
        raise ApiError.unauthenticated(exc.reason, expired=exc.expired) from exc

    user = await UserRepository(session).by_id(claims.user_id)
    if user is None or not user.is_active:
        # A token that verifies for an account that is gone or disabled. The
        # signature was genuine, so this is not an attack -- it is a session
        # that outlived its account, and it ends here.
        log.info("auth.token_for_unusable_account", user_id=claims.user_id)
        raise ApiError.unauthenticated("This account can no longer sign in.")

    # Every log line for the rest of this request carries who made it -- the
    # same contextvar mechanism that binds research_id through the engine, and
    # the reason an operator reading logs during an incident can tell whose
    # requests they are looking at without every call site passing an id.
    bind_research_context(user_id=user.id)

    return CurrentUser(id=user.id, email=user.email, display_name=user.display_name)


async def get_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[CurrentUser, Depends(current_user)],
) -> ResearchRepository:
    """A repository that can only see this user's research."""
    return ResearchRepository(session, user.viewer)


async def get_users(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserRepository:
    """The account repository. Unscoped, because it is what establishes scope."""
    return UserRepository(session)


Authenticated = Annotated[CurrentUser, Depends(current_user)]
Repository = Annotated[ResearchRepository, Depends(get_repository)]
Users = Annotated[UserRepository, Depends(get_users)]
