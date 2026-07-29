"""PostgreSQL scalar functions that SQLite lacks.

The test suite runs on SQLite while production runs on PostgreSQL 17.  Rather
than let the two schemas drift — writing ``trim()`` in the ORM and ``btrim()``
in the migration, and hoping they stay equivalent — the CHECK constraints in
:mod:`app.models` are written *exactly* as the adjudicated DDL writes them and
the handful of missing functions are registered on SQLite connections here.

SQLite resolves function names when it parses DDL, so registration must happen
on ``connect``, before ``CREATE TABLE`` runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import event

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def _btrim(value: str | None, chars: str | None = None) -> str | None:
    """PostgreSQL ``btrim``: strip *chars* (default whitespace) from both ends."""
    if value is None:
        return None
    return value.strip() if chars is None else value.strip(chars)


def install_sqlite_compat(engine: Engine) -> None:
    """Register PostgreSQL-compatible functions on a SQLite engine.

    A no-op for every other dialect.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _register(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.create_function("btrim", 1, _btrim)
        dbapi_connection.create_function("btrim", 2, _btrim)
