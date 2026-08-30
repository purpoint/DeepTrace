"""How a connection string reaches two drivers that disagree about it.

One `DATABASE_URL` is configured. The application reads it through asyncpg and
LangGraph's checkpointer reads it through psycopg, and the two spell the SSL
parameter differently -- so whichever spelling the operator was handed, each
driver has to see its own.

This is not a hypothetical. Every managed Postgres hands out a string ending in
`?sslmode=require`, and pasting one in produced `TypeError: connect() got an
unexpected keyword argument 'sslmode'` -- an error naming a keyword argument
rather than SSL, which sends the reader into the wrong file entirely.
"""

from __future__ import annotations

import pytest

from infrastructure.db.checkpointer import psycopg_url
from infrastructure.db.engine import normalise_database_url

pytestmark = pytest.mark.unit

MANAGED = (
    "postgresql://u:p@ep-x.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)
"""A Neon connection string, verbatim in shape. Both parameters are libpq's
and asyncpg rejects both, one deploy at a time."""


class TestTheDriverGetsItsOwnSpelling:
    def test_asyncpg_is_given_ssl(self) -> None:
        assert normalise_database_url(MANAGED) == (
            "postgresql+asyncpg://u:p@ep-x.us-east-2.aws.neon.tech/neondb?ssl=require"
        )

    def test_asyncpg_does_not_receive_what_it_cannot_take(self) -> None:
        """`channel_binding` has no asyncpg equivalent, so it is dropped rather
        than renamed. Fixing `sslmode` alone left this one to be discovered by
        the next deploy, which is the reason the whole family is handled at
        once rather than one parameter per failure."""
        assert "channel_binding" not in normalise_database_url(MANAGED)

    def test_psycopg_keeps_channel_binding(self) -> None:
        """It reads the raw setting, not the asyncpg rewrite, so the reduction
        is confined to one driver's connection."""
        assert "channel_binding=require" in psycopg_url(MANAGED)

    def test_psycopg_is_given_sslmode(self) -> None:
        """Including when the URL has already been through the asyncpg
        rewrite, which is how a caller passing an explicit url reaches it."""
        assert "sslmode=require" in psycopg_url(normalise_database_url(MANAGED))
        assert "ssl=require" not in psycopg_url(normalise_database_url(MANAGED))

    @pytest.mark.parametrize("mode", ["require", "verify-full", "disable", "prefer"])
    def test_the_value_is_carried_across_unchanged(self, mode: str) -> None:
        """Renamed, not reinterpreted. Downgrading verify-full to require would
        be a silent weakening of exactly the check it asks for."""
        url = f"postgresql://u:p@h/db?sslmode={mode}"

        assert normalise_database_url(url).endswith(f"?ssl={mode}")
        assert psycopg_url(normalise_database_url(url)).endswith(f"?sslmode={mode}")


class TestNothingElseIsDisturbed:
    def test_a_parameter_with_no_asyncpg_quarrel_survives(self) -> None:
        url = "postgresql://u:p@h/db?sslmode=require&application_name=deeptrace"
        rewritten = normalise_database_url(url)

        assert "application_name=deeptrace" in rewritten
        assert "ssl=require" in rewritten
        assert "sslmode" not in rewritten

    def test_connect_timeout_is_renamed_rather_than_dropped(self) -> None:
        """asyncpg spells it `timeout`. Dropping it would silently remove a
        ceiling somebody set on purpose."""
        rewritten = normalise_database_url("postgresql://u:p@h/db?connect_timeout=10")

        assert rewritten.endswith("?timeout=10")

    def test_a_url_with_no_query_is_untouched(self) -> None:
        assert normalise_database_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"

    def test_a_local_url_still_works(self) -> None:
        """The development case, which has no SSL parameter at all and is the
        one that must not regress."""
        assert normalise_database_url("postgresql://localhost:5432/deeptrace") == (
            "postgresql+asyncpg://localhost:5432/deeptrace"
        )

    def test_an_already_async_url_keeps_its_scheme(self) -> None:
        url = "postgresql+asyncpg://u:p@h/db?sslmode=require"

        assert normalise_database_url(url).startswith("postgresql+asyncpg://")
        assert "ssl=require" in normalise_database_url(url)
