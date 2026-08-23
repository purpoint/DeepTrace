"""Accounts: creating them, finding them, and recording that they signed in.

Small, and deliberately separate from the research repository. That one is
scoped to a viewer because everything it reads belongs to someone; this one is
what *establishes* who the viewer is, and cannot be scoped by the answer it
produces.

Email normalisation happens here rather than at the route, for the same reason
ownership filtering happens in the repository rather than at the route: a rule
enforced at the caller is a rule the next caller can skip. Normalising on the
way in is what makes the unique index a real constraint instead of a suggestion.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from core.observability.recorder import new_run_id
from infrastructure.db.models import User

log = get_logger(__name__)


class EmailAlreadyRegistered(Exception):
    """An account already exists for this address."""


def normalise_email(email: str) -> str:
    """Lowercase and strip. The form that goes in the column and in the index.

    Only the case and surrounding whitespace. The other normalisations people
    reach for -- stripping dots, cutting at a ``+`` -- are Gmail's rules, not
    the internet's, and applying them to a domain that treats those characters
    as significant merges two different people into one account.
    """
    return email.strip().lower()


class UserRepository:
    """Reads and writes accounts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        email: str,
        *,
        password_hash: str | None = None,
        display_name: str | None = None,
    ) -> User:
        """Register an account.

        The duplicate check is the database's unique index, not a preceding
        SELECT. A check-then-insert has a window between the two in which
        another request inserts the same address, and two people racing to
        register one email is exactly the case a constraint exists to decide.
        """
        user = User(
            id=new_run_id("usr"),
            email=normalise_email(email),
            password_hash=password_hash,
            display_name=(display_name or "").strip() or None,
        )
        self.session.add(user)

        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise EmailAlreadyRegistered(email) from exc

        log.info("auth.user_created", user_id=user.id)
        return user

    async def by_email(self, email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.email == normalise_email(email))
        )
        return result.scalar_one_or_none()

    async def by_id(self, user_id: str) -> User | None:
        result = await self.session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def set_password_hash(self, user_id: str, password_hash: str) -> None:
        """Store a new hash. Used on password change and on rehash-at-login."""
        await self.session.execute(
            update(User).where(User.id == user_id).values(password_hash=password_hash)
        )

    async def record_login(self, user_id: str) -> None:
        """Stamp the last successful sign-in.

        Not an audit log -- one timestamp cannot be one -- but enough for a
        person to notice a sign-in they do not recognise, which is the cheapest
        useful thing this column can do.
        """
        await self.session.execute(
            update(User).where(User.id == user_id).values(last_login_at=datetime.now(UTC))
        )


__all__ = ["EmailAlreadyRegistered", "UserRepository", "normalise_email"]
