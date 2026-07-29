"""Media schema v3 — greenfield.

This is a RESTRUCTURE, not a migration.  There is no production data worth
preserving and the derived images we hold are not ours, so the schema is
dropped and recreated rather than extended: ``ImageVersion`` conflated five
unrelated concerns and no additive change could unpick them.

The DDL is not written inline.  It lives beside this file as
``media_schema_v3.sql`` and is executed verbatim, so the shipped schema can be
diffed against the adjudicated artifact instead of being re-typed into Python
and drifting.  Exactly one line differs from that artifact — the ``media_app``
role is created idempotently instead of DROP+CREATE, because a role is a
cluster-wide object and dropping it from inside a migration fails when it owns
objects in any other database.  The deviation is marked in the .sql itself.

The DDL goes to a raw DBAPI cursor rather than through ``op.execute`` or
``exec_driver_sql``.  Both of those layers rewrite the statement: ``text()``
reads ``:`` sequences as bind parameters (the DDL is full of ``::text`` casts
and ``$$``-quoted bodies), and psycopg2 applies ``%``-interpolation whenever a
parameter collection is passed — which would mangle the two ``%`` characters
in the trigger bodies' ``RAISE EXCEPTION`` format strings.  ``cursor.execute``
with no parameters at all is the only path that ships the file byte-for-byte.

Revision ID: 0002_media_schema_v3
Revises: none — this replaces the whole chain
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0002_media_schema_v3"
down_revision = None
branch_labels = None
depends_on = None

_DDL = Path(__file__).with_name("media_schema_v3.sql")


def _run(sql: str) -> None:
    raw = op.get_bind().connection.dbapi_connection
    with raw.cursor() as cur:  # type: ignore[union-attr]
        cur.execute(sql)


def upgrade() -> None:
    # gen_random_uuid() and sha256() are in pgcrypto on PG12; core since PG13.
    # Requested explicitly so the DDL does not depend on which it is.
    _run("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    _run(_DDL.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Symmetrical with the "drop and recreate" premise: there is nothing to
    # preserve, and every object lives in the one schema.
    _run("DROP SCHEMA IF EXISTS media CASCADE")
