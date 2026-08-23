"""Passwords: hashed with Argon2id, verified without leaking timing.

Three decisions, each of which is a known way to get this wrong.

*Argon2id, not a general-purpose hash.* SHA-256 is designed to be fast, which
is precisely the property an attacker with a stolen table wants. Argon2id is
designed to be slow and memory-hungry, so a GPU farm's advantage over a laptop
shrinks from thousands of times to single digits. It won the Password Hashing
Competition and is what RFC 9106 recommends.

*Hashing runs in a thread.* An Argon2 verification deliberately costs tens of
milliseconds and 64 MiB. On the event loop that is not slow, it is *stopped* --
every other request in the process waits, and a handful of concurrent logins
stalls the whole service. ``asyncio.to_thread`` is what keeps a cost that must
be paid from being paid by everyone.

*A failed login costs the same as a successful one.* Verification against a
non-existent account skips straight to "no" in the obvious implementation, and
returns in a microsecond instead of eighty milliseconds -- which tells anyone
watching the clock exactly which email addresses are registered. So a lookup
that finds nothing still hashes something.
"""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_errors

from core.logging import get_logger

log = get_logger(__name__)

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # KiB, so 64 MiB per hash
    parallelism=4,
    hash_len=32,
    salt_len=16,
)
"""The cost parameters, stated rather than defaulted.

They are encoded into every hash this produces, which is what makes them
changeable: raising the cost does not invalidate existing hashes, because each
one carries the parameters it was made with and is re-hashed at the owner's
next successful login. Written out here so that raising them is a visible edit
rather than a library upgrade nobody noticed.
"""

_DUMMY_HASH = _hasher.hash("a password no account has")
"""Something to verify against when there is no account.

Computed once at import, because computing it per request would double the cost
of every failed login -- and the reason it exists is to make failed logins cost
the *same*, not more.
"""


async def hash_password(password: str) -> str:
    """Hash a password for storage. Never returns the same string twice.

    The salt is random per call, which is why two people with the same password
    get different hashes -- and why a stolen table cannot be sorted to find
    them.
    """
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password: str, stored_hash: str | None) -> tuple[bool, str | None]:
    """Check a password, and say whether its hash should be rewritten.

    Returns ``(matched, new_hash)``. The second value is present only when the
    password was correct *and* the stored hash was made with weaker parameters
    than the ones configured now -- the only moment the plaintext is available
    to upgrade it. Ignoring it means a policy change that never reaches the
    accounts that predate it.

    ``stored_hash`` of ``None`` is an account with no password: created by the
    CLI to own a run, or belonging to an identity provider. It cannot be signed
    into, and the dummy verification below is what makes that indistinguishable
    from a wrong password.
    """
    target = stored_hash or _DUMMY_HASH

    try:
        await asyncio.to_thread(_hasher.verify, target, password)
    except argon2_errors.VerificationError:
        return False, None
    except argon2_errors.InvalidHashError:
        # A stored value that is not an Argon2 hash at all. Corruption, or a
        # migration from another format that did not finish. Refuse the login
        # rather than crash the endpoint, and log it: a user who cannot sign in
        # deserves an operator who can see why.
        log.error("auth.hash_unreadable")
        return False, None

    if stored_hash is None:
        # The dummy matched, which means someone guessed the placeholder. The
        # account still has no password and still cannot be signed into.
        return False, None

    if _hasher.check_needs_rehash(stored_hash):
        return True, await hash_password(password)
    return True, None


class PasswordPolicyError(ValueError):
    """A password that will not be accepted, with the reason a person needs."""


def check_policy(password: str, *, minimum: int, maximum: int) -> None:
    """Reject a password that is too short, or long enough to be an attack.

    The lower bound is the only strength rule here. Composition rules -- one
    digit, one symbol -- push people toward ``Password1!`` and measurably do not
    help; length is what actually costs an attacker something.

    The upper bound is not a strength rule at all. Argon2 is intentionally
    expensive, so an unbounded field lets anyone make this server hash a
    megabyte per request, as many times as they like.
    """
    if len(password) < minimum:
        raise PasswordPolicyError(f"Use at least {minimum} characters.")
    if len(password) > maximum:
        raise PasswordPolicyError(f"Use at most {maximum} characters.")


__all__ = ["PasswordPolicyError", "check_policy", "hash_password", "verify_password"]
