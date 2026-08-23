"""Who is asking, expressed as something a query can use.

The requirement is that user A cannot read user B's research, and that this is
true *at the query layer* rather than at the route. The distinction is not
pedantry. A check in a route is a line of code that a new route can forget to
write, and the failure mode of forgetting is silent: the endpoint works, the
tests pass, and it serves other people's data.

So the repository does not take an optional ``user_id`` it might filter on. It
takes a :class:`Viewer`, it takes it as a required constructor argument, and
every read it performs narrows to that viewer. A route cannot forget to pass one
because the repository cannot be built without one.

The unscoped path still has to exist -- the worker saves runs on behalf of
whoever queued them, and the CLI has no user at all -- so it exists as
:meth:`Viewer.system`, which is a name that appears in a diff and can be grepped
for. An implicit ``None`` meaning "see everything" is the same power with none
of the visibility.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Viewer:
    """The identity a repository reads on behalf of."""

    user_id: str | None
    trusted: bool = False
    """Whether this viewer bypasses ownership filtering.

    True only for internal callers that are not acting for a person: the worker
    persisting a finished run, the CLI, a migration. Never derived from a
    request -- there is no header, claim, or role that sets this, because the
    only safe way to obtain it is to construct it in code that a reviewer read.
    """

    @classmethod
    def system(cls) -> Viewer:
        """An internal caller. Sees everything, owns nothing."""
        return cls(user_id=None, trusted=True)

    @classmethod
    def user(cls, user_id: str) -> Viewer:
        """A person, who sees exactly their own research."""
        return cls(user_id=user_id)

    @property
    def is_system(self) -> bool:
        return self.trusted

    def owns(self, owner_id: str | None) -> bool:
        """Whether this viewer may see a row owned by ``owner_id``.

        A row with no owner belongs to nobody and is therefore visible to
        nobody but the system. That covers research created before accounts
        existed and research created by the CLI -- both of which are real, and
        neither of which should appear in a stranger's history because ``None``
        happened to compare equal to a missing claim somewhere.
        """
        if self.trusted:
            return True
        return owner_id is not None and owner_id == self.user_id


__all__ = ["Viewer"]
