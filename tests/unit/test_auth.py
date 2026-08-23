"""Unit tests for the authentication primitives.

No database and no Redis: hashing and signing are local computations, and the
properties worth pinning here are properties of those computations. What a
stolen token can do, what a wrong password reveals, and what a verifier accepts
when it is not being careful.

Each test names the attack it prevents rather than the function it calls. A test
called ``test_read_rejects_bad_typ`` describes the code; ``a refresh token
cannot be used as an access token`` describes what goes wrong when it does not.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from core.config import MissingConfigurationError, Settings
from infrastructure.auth.passwords import (
    PasswordPolicyError,
    check_policy,
    hash_password,
    verify_password,
)
from infrastructure.auth.tokens import (
    InvalidToken,
    TokenKind,
    mint,
    read,
)

SECRET = "a-test-signing-key-long-enough-to-pass-validation"


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, jwt_secret=SECRET, **overrides)  # type: ignore[arg-type,call-arg]


class TestPasswords:
    async def test_a_password_verifies_against_its_own_hash(self) -> None:
        stored = await hash_password("correct horse battery staple")

        matched, _ = await verify_password("correct horse battery staple", stored)

        assert matched

    async def test_a_wrong_password_does_not(self) -> None:
        stored = await hash_password("correct horse battery staple")

        matched, _ = await verify_password("correct horse battery stapler", stored)

        assert not matched

    async def test_the_same_password_hashes_differently_every_time(self) -> None:
        """A random salt per hash. Without it, two people who chose the same
        password have the same row, and a stolen table can be sorted to find
        which ones."""
        first = await hash_password("a shared password")
        second = await hash_password("a shared password")

        assert first != second

    async def test_the_hash_records_the_algorithm_and_its_cost(self) -> None:
        """The parameters travel with the hash, which is what makes raising them
        possible without invalidating every existing account."""
        stored = await hash_password("a password")

        assert stored.startswith("$argon2id$")
        assert "m=65536" in stored
        assert "t=3" in stored

    async def test_an_account_with_no_password_cannot_be_signed_into(self) -> None:
        """A CLI-created account has no password hash. That must read as a
        failed verification rather than as a check that was skipped."""
        matched, _ = await verify_password("anything at all", None)

        assert not matched

    async def test_verifying_against_no_account_costs_the_same_as_a_real_check(self) -> None:
        """The timing channel. If a missing account returned in a microsecond
        while a real one took eighty milliseconds, the response time would
        answer the question the error message refuses to: is this address
        registered?
        """
        stored = await hash_password("a password")

        started = time.perf_counter()
        await verify_password("a guess", stored)
        real = time.perf_counter() - started

        started = time.perf_counter()
        await verify_password("a guess", None)
        missing = time.perf_counter() - started

        # Generously loose: the claim is "the same order of magnitude", not
        # "identical". A test that demanded identical timings would fail on a
        # busy machine and teach everyone to ignore it.
        assert missing > real / 4

    async def test_a_corrupt_hash_refuses_the_login_rather_than_crashing(self) -> None:
        matched, _ = await verify_password("a password", "not an argon2 hash at all")

        assert not matched

    def test_a_short_password_is_refused(self) -> None:
        with pytest.raises(PasswordPolicyError):
            check_policy("short", minimum=12, maximum=1024)

    def test_an_enormous_password_is_refused(self) -> None:
        """Not a strength rule. Argon2 is deliberately expensive, so an
        unbounded field is a way to make the server hash a megabyte on
        demand."""
        with pytest.raises(PasswordPolicyError):
            check_policy("x" * 5000, minimum=12, maximum=1024)

    def test_a_long_passphrase_with_no_symbols_is_accepted(self) -> None:
        """Length is the rule. Composition requirements push people toward
        'Password1!', which is shorter and easier to guess."""
        check_policy("the quick brown fox jumps", minimum=12, maximum=1024)


class TestTokens:
    def test_a_minted_token_reads_back_as_what_it_was(self) -> None:
        minted = mint("usr_1", TokenKind.ACCESS, settings())

        claims = read(minted.token, TokenKind.ACCESS, settings())

        assert claims.user_id == "usr_1"
        assert claims.kind is TokenKind.ACCESS
        assert claims.token_id == minted.token_id

    def test_a_refresh_token_cannot_be_used_as_an_access_token(self) -> None:
        """The substitution bug. Both tokens are signed with the same key by the
        same service, so a verifier that does not compare ``typ`` accepts either
        -- and the access token's fifteen-minute exposure window silently
        becomes the refresh token's fourteen days.
        """
        refresh = mint("usr_1", TokenKind.REFRESH, settings())

        with pytest.raises(InvalidToken):
            read(refresh.token, TokenKind.ACCESS, settings())

    def test_an_access_token_cannot_be_used_to_refresh(self) -> None:
        """The same check in the other direction, which matters because an
        access token is presented far more often and therefore leaks more
        easily -- turning one into a fourteen-day session would undo the
        reason the two are separate."""
        access = mint("usr_1", TokenKind.ACCESS, settings())

        with pytest.raises(InvalidToken):
            read(access.token, TokenKind.REFRESH, settings())

    def test_a_token_signed_with_another_key_is_refused(self) -> None:
        minted = mint("usr_1", TokenKind.ACCESS, settings())
        other = Settings(_env_file=None, jwt_secret="a-different-key-also-long-enough-to-pass")  # type: ignore[call-arg]

        with pytest.raises(InvalidToken):
            read(minted.token, TokenKind.ACCESS, other)

    def test_an_unsigned_token_is_refused(self) -> None:
        """``alg: none``. A verifier that reads the algorithm out of the token
        it is verifying will accept a token anyone can write, which is why the
        algorithm is passed in rather than read out.
        """
        forged = jwt.encode(
            {
                "sub": "usr_admin",
                "jti": "forged",
                "typ": "access",
                "iss": "deeptrace",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            key="",
            algorithm="none",
        )

        with pytest.raises(InvalidToken):
            read(forged, TokenKind.ACCESS, settings())

    def test_a_token_from_another_issuer_is_refused(self) -> None:
        """A staging environment, a sibling service, or an old key that was
        reused. Same signature, different system."""
        elsewhere = jwt.encode(
            {
                "sub": "usr_1",
                "jti": "abc",
                "typ": "access",
                "iss": "some-other-service",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(InvalidToken):
            read(elsewhere, TokenKind.ACCESS, settings())

    def test_an_expired_token_says_so(self) -> None:
        """Expiry is reported separately, because it is the one failure a client
        should handle silently -- refresh and retry, not sign in again. It
        discloses nothing: the expiry is in the payload the holder already has.
        """
        expired = jwt.encode(
            {
                "sub": "usr_1",
                "jti": "abc",
                "typ": "access",
                "iss": "deeptrace",
                "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
                "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(InvalidToken) as raised:
            read(expired, TokenKind.ACCESS, settings())

        assert raised.value.expired

    def test_a_token_missing_a_required_claim_is_refused(self) -> None:
        """Every claim this system reads is required. A token without ``exp``
        would otherwise verify and never expire."""
        incomplete = jwt.encode(
            {"sub": "usr_1", "jti": "abc", "typ": "access", "iss": "deeptrace"},
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(InvalidToken):
            read(incomplete, TokenKind.ACCESS, settings())

    def test_two_tokens_for_one_user_are_separately_identifiable(self) -> None:
        """Each carries its own id, which is what makes revoking one session
        without touching the others possible."""
        first = mint("usr_1", TokenKind.REFRESH, settings())
        second = mint("usr_1", TokenKind.REFRESH, settings())

        assert first.token_id != second.token_id

    def test_minting_without_a_signing_key_fails_by_name(self) -> None:
        unconfigured = Settings(_env_file=None)  # type: ignore[call-arg]

        with pytest.raises(MissingConfigurationError) as raised:
            mint("usr_1", TokenKind.ACCESS, unconfigured)

        assert raised.value.field == "jwt_secret"


class TestTheSigningKeyPolicy:
    def test_a_short_key_is_rejected_at_startup(self) -> None:
        """HS256 keys are brute-forceable offline -- an attacker with one token
        can try candidates as fast as their hardware allows, with no server
        involved and nothing to rate limit. Refused at startup, because the
        moment to catch it is before anyone has signed in with it."""
        with pytest.raises(ValueError, match="at least 32 characters"):
            Settings(_env_file=None, jwt_secret="too-short")  # type: ignore[call-arg]

    def test_an_empty_key_is_treated_as_unset(self) -> None:
        """``JWT_SECRET=`` in a .env file is a deployment that has not configured
        signing yet, not a two-character key. It should fail at the point of use
        with a message naming the variable, not at startup with one about
        length."""
        assert Settings(_env_file=None, jwt_secret="").jwt_secret is None  # type: ignore[call-arg]
