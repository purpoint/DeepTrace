"""JSON Web Tokens: minting them, and refusing the ones that only look right.

Two kinds are issued, and the difference between them is the whole design.

An **access token** is presented on every request and verified by signature
alone -- no database, no Redis. That is what makes the API readable during a
Redis outage and cheap under load, and it is also why it cannot be revoked:
nothing is consulted that could say no. So it lives fifteen minutes, and that
number *is* the exposure window for a stolen one.

A **refresh token** is presented rarely, to obtain a new access token, and is
checked against a server-side record (see :mod:`infrastructure.auth.sessions`).
Because something is consulted, it can be revoked -- which is what makes
"sign out" mean something and lets a long-lived session be affordable.

Three ways to get JWT verification wrong, all of them closed here:

*Trusting the token's own algorithm.* A JWT header names its algorithm, and a
verifier that reads it will happily accept ``alg: none`` -- an unsigned token
that anyone can write. The algorithm is passed in, not read out.

*Letting the two kinds substitute for each other.* If ``typ`` is not checked, a
refresh token works as an access token, and the fifteen-minute exposure window
quietly becomes fourteen days. The claim is required and compared.

*Accepting anyone's signature.* ``iss`` is checked so that a token minted by
something else that shares this secret -- a staging deployment, a sibling
service, an old key that was reused -- is not honoured here.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import jwt

from core.config import Settings

ALGORITHM = "HS256"
"""Symmetric, because there is one service and it both signs and verifies.

RS256 exists so a verifier can check a signature without being able to forge
one, which matters when verification happens somewhere the signing key must not
go. Nothing here is in that position, and an asymmetric key pair would be
ceremony without a beneficiary.
"""


class TokenKind(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class InvalidToken(Exception):
    """A token that will not be honoured, and whether it was merely stale.

    ``expired`` is separated because it is the one failure a client should act
    on differently: an expired access token means "refresh and retry", while
    anything else means "sign in again". It is not a disclosure -- the expiry is
    written in the token's own payload, which the holder can read.
    """

    def __init__(self, reason: str, *, expired: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.expired = expired


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """What a verified token asserts."""

    user_id: str
    token_id: str
    kind: TokenKind
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A freshly signed token, and the parts a caller needs to record."""

    token: str
    token_id: str
    expires_at: datetime

    @property
    def expires_in(self) -> int:
        """Seconds of remaining life, for a client that would rather not parse."""
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


def mint(user_id: str, kind: TokenKind, settings: Settings) -> MintedToken:
    """Sign a token of the given kind for the given user."""
    secret = settings.require("jwt_secret")
    lifetime = (
        settings.access_token_ttl_seconds
        if kind is TokenKind.ACCESS
        else settings.refresh_token_ttl_seconds
    )

    issued = datetime.now(UTC)
    expires = issued + timedelta(seconds=lifetime)
    token_id = secrets.token_urlsafe(16)

    payload = {
        "sub": user_id,
        "jti": token_id,
        "typ": kind.value,
        "iss": settings.jwt_issuer,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return MintedToken(
        token=jwt.encode(payload, secret, algorithm=ALGORITHM),
        token_id=token_id,
        expires_at=expires,
    )


def read(token: str, expected: TokenKind, settings: Settings) -> TokenClaims:
    """Verify a token and return what it claims, or refuse it.

    Every failure leaves here as :class:`InvalidToken`. PyJWT raises a dozen
    different exceptions, and a caller that handles some of them has written a
    401 for the cases it thought of and a 500 for the rest.
    """
    secret = settings.require("jwt_secret")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "jti", "typ", "iss", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidToken("This token has expired.", expired=True) from exc
    except jwt.InvalidTokenError as exc:
        # Covers a bad signature, a wrong issuer, a missing claim, and a
        # payload that is not a JWT at all. Reported to the caller as one thing,
        # because "which part was wrong" is only useful to someone forging it.
        raise InvalidToken("This token is not valid.") from exc

    kind = payload["typ"]
    if kind != expected.value:
        raise InvalidToken(f"Expected a {expected.value} token.")

    return TokenClaims(
        user_id=str(payload["sub"]),
        token_id=str(payload["jti"]),
        kind=TokenKind(kind),
        issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )


__all__ = [
    "ALGORITHM",
    "InvalidToken",
    "MintedToken",
    "TokenClaims",
    "TokenKind",
    "mint",
    "read",
]
