from __future__ import annotations

from typing import TYPE_CHECKING, Any, Union

from sqlalchemy import func, or_

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session
    from sqlalchemy.orm.attributes import InstrumentedAttribute
    from sqlalchemy.sql.elements import ColumnElement

#: Callers pass mapped columns (``Asset.mime``), which are
#: ``InstrumentedAttribute`` rather than plain ``ColumnElement``.  Both carry
#: the operators used below.
SearchColumn = Union["ColumnElement[Any]", "InstrumentedAttribute[Any]"]


def is_postgres(db: Session) -> bool:
    """Return True if the session is backed by PostgreSQL."""
    return db.bind.dialect.name == "postgresql"  # type: ignore[union-attr]


def tsvector_concat(*cols: SearchColumn) -> ColumnElement[Any]:
    return func.to_tsvector("simple", func.coalesce(func.concat_ws(" ", *cols), ""))


def ts_query(query: str) -> ColumnElement[Any]:
    return func.plainto_tsquery("simple", query)


def build_text_filter(db: Session, query: str, *cols: SearchColumn) -> ColumnElement[bool]:
    """Build a text-search filter clause.

    On PostgreSQL: uses to_tsvector / plainto_tsquery for proper full-text search.
    On other engines (SQLite, etc.): falls back to ILIKE substring matching.
    """
    if is_postgres(db):
        vector = tsvector_concat(*cols)
        return vector.op("@@")(ts_query(query))
    # Fallback: ILIKE on each column
    pattern = f"%{query}%"
    return or_(*(col.ilike(pattern) for col in cols))


def tags_any_match(col_tags: SearchColumn, tags: Iterable[str]) -> ColumnElement[Any]:
    # expects an array column; use overlap operator
    arr = func.ARRAY(list(tags))
    return col_tags.op("&&")(arr)
