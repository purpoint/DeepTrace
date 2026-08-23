"""Server-side session records: what makes a token revocable.

A signed token proves who minted it and when it expires, and nothing else. It
cannot be withdrawn, because verifying it consults nothing that could refuse.
This module is the thing that refuses.

Only refresh tokens are recorded, which is deliberate. Recording access tokens
too would mean a Redis lookup on every single request, and a Redis outage would
log out everyone in the building; recording neither would mean "sign out" is a
button that clears local storage and leaves a working credential in whatever
copied it. Recording the rare one buys revocation at almost no cost.

**Rotation.** A refresh token is single-use: presenting it consumes the record
and a new pair is issued. That bounds how long a stolen one is worth anything,
and it creates the signal below.

**Reuse detection.** If a refresh token is presented whose record is gone, one
of two things happened: the legitimate holder already rotated it and someone
else has a copy, or the reverse. There is no way to tell which party is which,
so both are logged out -- every session for that user is destroyed, and the
person has to sign in with a password they alone know.

**The grace window.** Strict reuse detection has a false positive that hurts:
two refreshes racing -- a client with two tabs, or a component that mounts
twice -- both present the same token, one wins, and the loser looks exactly
like a thief. So a consumed token id is remembered briefly, and a second
presentation inside that window is merely refused rather than treated as theft.
The cost is stated plainly: a thief who uses a stolen token within the window
escapes detection. Thirty seconds of that is worth not signing people out of
their own accounts for opening a second tab.
"""

from __future__ import annotations

import secrets

from redis.asyncio import Redis

from core.logging import get_logger

log = get_logger(__name__)

REFRESH_KEY = "deeptrace:auth:refresh:{token_id}"
FAMILY_KEY = "deeptrace:auth:sessions:{user_id}"
GRACE_KEY = "deeptrace:auth:rotated:{token_id}"
TICKET_KEY = "deeptrace:auth:ticket:{ticket}"

GRACE_SECONDS = 30
"""How long a just-rotated token is refused rather than treated as stolen."""


class ReusedToken(Exception):
    """A refresh token was presented after it had already been spent.

    Distinct from an unknown token because the response is different: this
    destroys every session the user has, and the user is told to sign in again.
    """


class SessionStore:
    """Refresh-token records and WebSocket tickets, in Redis.

    Both are short-lived credentials with a server-side record, which is the
    only thing they have in common -- and enough to make one small class rather
    than two nearly identical ones.
    """

    def __init__(self, client: Redis) -> None:
        self.client = client

    # -- refresh tokens ----------------------------------------------------

    async def remember(self, user_id: str, token_id: str, ttl_seconds: int) -> None:
        """Record a refresh token as live, and file it under its owner.

        The family set is what makes "sign out everywhere" possible: without it,
        revoking a user's sessions would mean scanning every key in Redis to
        find which ones are theirs.
        """
        family = FAMILY_KEY.format(user_id=user_id)
        pipeline = self.client.pipeline()
        pipeline.set(REFRESH_KEY.format(token_id=token_id), user_id, ex=ttl_seconds)
        pipeline.sadd(family, token_id)
        # The set outlives its longest member by a margin, so a user whose last
        # token expires does not leave an immortal empty set behind.
        pipeline.expire(family, ttl_seconds + GRACE_SECONDS)
        await pipeline.execute()

    async def spend(self, user_id: str, token_id: str) -> None:
        """Consume a refresh token, or explain why it cannot be consumed.

        Returns nothing on success. Raises :class:`ReusedToken` when the token
        was already spent outside the grace window -- the theft signal -- and
        :class:`LookupError` when it is simply not a token this server knows,
        which is what a revoked or long-gone session looks like.

        ``GETDEL`` rather than a read followed by a delete: two clients racing
        on a read-then-delete both see the token as live and both succeed, which
        is exactly the single-use property this is here to provide.
        """
        owner = await self.client.getdel(REFRESH_KEY.format(token_id=token_id))

        if owner is not None:
            if _text(owner) != user_id:
                # The signature said one user and the record says another. That
                # is not a race; it is a token that has been tampered with or a
                # key that has been reused across deployments.
                log.error("auth.refresh_owner_mismatch", user_id=user_id)
                raise LookupError("This session is not recognised.")

            pipeline = self.client.pipeline()
            pipeline.set(GRACE_KEY.format(token_id=token_id), user_id, ex=GRACE_SECONDS)
            pipeline.srem(FAMILY_KEY.format(user_id=user_id), token_id)
            await pipeline.execute()
            return

        if await self.client.exists(GRACE_KEY.format(token_id=token_id)):
            # Spent moments ago. Almost certainly the same client asking twice;
            # refused, but not treated as evidence of anything.
            raise LookupError("This session was just refreshed. Use the newer token.")

        raise ReusedToken("This session token has already been used.")

    async def forget(self, user_id: str, token_id: str) -> None:
        """Revoke one refresh token. What signing out does."""
        pipeline = self.client.pipeline()
        pipeline.delete(REFRESH_KEY.format(token_id=token_id))
        pipeline.srem(FAMILY_KEY.format(user_id=user_id), token_id)
        await pipeline.execute()

    async def revoke_all(self, user_id: str) -> int:
        """Destroy every session a user has. Returns how many there were.

        Called on reuse detection, and available for the moment a person says
        their laptop was stolen.
        """
        family = FAMILY_KEY.format(user_id=user_id)
        token_ids = {_text(value) for value in await self.client.smembers(family)}

        pipeline = self.client.pipeline()
        for token_id in token_ids:
            pipeline.delete(REFRESH_KEY.format(token_id=token_id))
        pipeline.delete(family)
        await pipeline.execute()

        log.warning("auth.sessions_revoked", user_id=user_id, count=len(token_ids))
        return len(token_ids)

    # -- WebSocket tickets -------------------------------------------------

    async def issue_ticket(self, user_id: str, ttl_seconds: int) -> str:
        """Mint a single-use ticket for opening a progress stream.

        A browser cannot set an ``Authorization`` header on a WebSocket -- the
        API simply has no place to put one -- so the credential has to travel in
        the URL. URLs are written to access logs, proxy logs, and browser
        history, which makes putting a fifteen-minute access token there a way
        of persisting it somewhere it was never meant to go.

        A ticket is the alternative: opaque, valid for seconds, and destroyed by
        the first use. What leaks into a log file is a string that stopped
        meaning anything before the log was rotated.
        """
        ticket = secrets.token_urlsafe(32)
        await self.client.set(TICKET_KEY.format(ticket=ticket), user_id, ex=ttl_seconds)
        return ticket

    async def redeem_ticket(self, ticket: str) -> str | None:
        """Exchange a ticket for the user it was issued to, exactly once."""
        owner = await self.client.getdel(TICKET_KEY.format(ticket=ticket))
        return _text(owner) if owner is not None else None


def _text(value: str | bytes) -> str:
    """Redis returns bytes unless the client was built to decode.

    Handled here rather than by requiring a decoding client, because this store
    shares its connection with the job queue and the queue's client is not
    something this module gets to configure.
    """
    return value.decode() if isinstance(value, bytes) else value


__all__ = ["GRACE_SECONDS", "ReusedToken", "SessionStore"]
