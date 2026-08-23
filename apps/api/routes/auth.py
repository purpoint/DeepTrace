"""Signing in, staying in, and stopping.

Six endpoints, and most of what is interesting about them is what they refuse
to tell you.

**Login says one thing for every failure.** No account, wrong password, and
disabled account all produce the same sentence. Distinguishing them turns the
endpoint into a service that reports whether an address is registered, which is
the first step of a credential-stuffing run and a privacy leak in its own right
-- knowing that someone has an account here is knowing something about them.

**Registration cannot hide the same fact**, and pretending otherwise would be
worse. An address that is already taken has to be refused, because the
alternative is silently doing nothing and telling the caller it worked. So the
enumeration is bounded rather than denied: the rate limit is what makes walking
a list of addresses expensive, and this is written down in the known gaps rather
than left as an implied claim of secrecy.

**Timing is levelled.** A login for an address with no account still performs an
Argon2 verification against a dummy hash, because returning in a microsecond
instead of eighty milliseconds answers the question the error message would not.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from apps.api.dependencies.identity import Authenticated, Users
from apps.api.dependencies.providers import ClientAddress, Config, Limiter, Sessions
from apps.api.errors import ApiError
from apps.api.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TicketResponse,
    TokenPair,
    UserView,
)
from core.config import Settings
from core.logging import get_logger
from infrastructure.auth.passwords import (
    PasswordPolicyError,
    check_policy,
    hash_password,
    verify_password,
)
from infrastructure.auth.sessions import ReusedToken, SessionStore
from infrastructure.auth.tokens import InvalidToken, TokenKind, mint, read
from infrastructure.db.repositories.users import EmailAlreadyRegistered
from infrastructure.rate_limit import RateLimiter

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SIGN_IN_FAILED = "That email and password do not match an account."
"""One sentence for every way signing in can fail. See the module docstring."""


async def _issue(user_id: str, settings: Settings, sessions: SessionStore) -> TokenPair:
    """Mint a pair and record the refresh half so it can be revoked."""
    access = mint(user_id, TokenKind.ACCESS, settings)
    refresh = mint(user_id, TokenKind.REFRESH, settings)
    await sessions.remember(user_id, refresh.token_id, settings.refresh_token_ttl_seconds)
    return TokenPair(
        access_token=access.token,
        refresh_token=refresh.token,
        expires_in=access.expires_in,
    )


async def _guard(limiter: RateLimiter, address: str, settings: Settings) -> None:
    """Apply the authentication rate limit, keyed by client address.

    Keyed by address rather than by the submitted email, because the attacker
    chooses the email. Counting per address is the only key an attacker cannot
    vary freely -- and its cost, that everyone behind one NAT shares a bucket,
    is the reason the limit is ten per fifteen minutes rather than three.
    """
    decision = await limiter.check(
        "auth",
        address,
        limit=settings.auth_rate_limit,
        window=settings.auth_rate_window_seconds,
    )
    if not decision.allowed:
        log.warning("auth.rate_limited", address=address)
        raise ApiError.rate_limited(decision.retry_after, limit=decision.limit)


@router.post(
    "/register",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def register(
    body: RegisterRequest,
    users: Users,
    sessions: Sessions,
    limiter: Limiter,
    address: ClientAddress,
    settings: Config,
) -> TokenPair:
    """Register, and return a signed-in session.

    Signing the new account in immediately rather than redirecting to a login
    form: the password was just verified by being chosen, and making someone
    type it again proves nothing to anyone.
    """
    await _guard(limiter, address, settings)

    try:
        check_policy(
            body.password,
            minimum=settings.password_min_length,
            maximum=settings.password_max_length,
        )
    except PasswordPolicyError as exc:
        raise ApiError.invalid(str(exc)) from exc

    password_hash = await hash_password(body.password)

    try:
        user = await users.create(
            body.email, password_hash=password_hash, display_name=body.display_name
        )
    except EmailAlreadyRegistered as exc:
        raise ApiError.conflict("An account already exists for that email address.") from exc

    return await _issue(user.id, settings, sessions)


@router.post("/login", response_model=TokenPair, summary="Sign in")
async def login(
    body: LoginRequest,
    users: Users,
    sessions: Sessions,
    limiter: Limiter,
    address: ClientAddress,
    settings: Config,
) -> TokenPair:
    """Exchange a password for a token pair."""
    await _guard(limiter, address, settings)

    user = await users.by_email(body.email)
    stored = user.password_hash if user else None

    # Runs even when there is no account, against a dummy hash. The cost is the
    # point: an unconditional eighty milliseconds is what stops the response
    # time from answering "is this address registered?".
    matched, rehashed = await verify_password(body.password, stored)

    if user is None or not matched or not user.is_active:
        log.info("auth.login_failed", address=address, known_account=user is not None)
        raise ApiError.unauthenticated(SIGN_IN_FAILED)

    if rehashed is not None:
        # The only moment the plaintext exists and the hash is known to be
        # stale. Skipping it means a cost increase never reaches the accounts
        # that predate it.
        await users.set_password_hash(user.id, rehashed)

    await users.record_login(user.id)
    log.info("auth.login", user_id=user.id)
    return await _issue(user.id, settings, sessions)


@router.post("/refresh", response_model=TokenPair, summary="Renew a session")
async def refresh(
    body: RefreshRequest,
    users: Users,
    sessions: Sessions,
    settings: Config,
) -> TokenPair:
    """Exchange a refresh token for a new pair, spending the old one.

    Not rate limited by address. A client with a valid refresh token is already
    known, its rate is bounded by the access token's lifetime, and limiting it
    would mean a shared office network can sign one person out at a time.
    """
    try:
        claims = read(body.refresh_token, TokenKind.REFRESH, settings)
    except InvalidToken as exc:
        raise ApiError.unauthenticated("Sign in again.", expired=exc.expired) from exc

    try:
        await sessions.spend(claims.user_id, claims.token_id)
    except ReusedToken as exc:
        # A validly signed refresh token whose record is gone, presented outside
        # the rotation grace window. Either the holder is a thief or the thief
        # already used the original -- and there is no way to tell which, so
        # every session for this account ends and the password becomes the only
        # way back in.
        revoked = await sessions.revoke_all(claims.user_id)
        log.warning("auth.refresh_reuse", user_id=claims.user_id, sessions_revoked=revoked)
        raise ApiError.unauthenticated("This session was ended for safety. Sign in again.") from exc
    except LookupError as exc:
        raise ApiError.unauthenticated("Sign in again.") from exc

    user = await users.by_id(claims.user_id)
    if user is None or not user.is_active:
        raise ApiError.unauthenticated("This account can no longer sign in.")

    return await _issue(user.id, settings, sessions)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
async def logout(
    body: RefreshRequest,
    user: Authenticated,
    sessions: Sessions,
    settings: Config,
) -> Response:
    """End one session.

    The refresh token is the thing being revoked, so it has to be presented --
    a logout that only clears the client's storage leaves a working credential
    in anything that copied it. The access token cannot be revoked and is not
    pretended to be: it expires on its own, within minutes.

    Requires authentication as well, so that a leaked refresh token cannot be
    used by a stranger to sign its owner out.
    """
    try:
        claims = read(body.refresh_token, TokenKind.REFRESH, settings)
    except InvalidToken:
        # Already expired or malformed. Nothing to revoke, and nothing worth
        # telling the caller: they asked to be signed out and they are.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if claims.user_id == user.id:
        await sessions.forget(user.id, claims.token_id)
        log.info("auth.logout", user_id=user.id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/logout-everywhere",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of every session",
)
async def logout_everywhere(user: Authenticated, sessions: Sessions) -> Response:
    """Revoke every refresh token this account has.

    What a person needs the moment they think a device is compromised. Access
    tokens already issued survive until they expire, which is the price of
    verifying them without a lookup -- minutes, not days.
    """
    await sessions.revoke_all(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserView, summary="The signed-in account")
async def me(user: Authenticated, users: Users) -> UserView:
    """Who the presented token belongs to. What a client calls on load."""
    row = await users.by_id(user.id)
    assert row is not None  # noqa: S101 -- current_user just loaded it
    return UserView(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        created_at=row.created_at,
        last_login_at=row.last_login_at,
    )


@router.post(
    "/ws-ticket", response_model=TicketResponse, summary="A ticket for the progress stream"
)
async def ws_ticket(user: Authenticated, sessions: Sessions, settings: Config) -> TicketResponse:
    """Mint a single-use, seconds-long credential for a WebSocket.

    A browser cannot attach an ``Authorization`` header when opening a
    WebSocket; the API has nowhere to put one. The credential therefore has to
    travel in the URL, and URLs are recorded -- in access logs, in proxy logs,
    in browser history. Putting a fifteen-minute access token there copies it
    into all of those.

    A ticket is the same idea with the lifetime shortened until the leak stops
    mattering: opaque, valid for thirty seconds, and destroyed by the first
    connection that uses it.
    """
    ticket = await sessions.issue_ticket(user.id, settings.ws_ticket_ttl_seconds)
    return TicketResponse(ticket=ticket, expires_in=settings.ws_ticket_ttl_seconds)


__all__ = ["router"]
