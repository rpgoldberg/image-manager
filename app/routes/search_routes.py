"""Search routes.

Asset cursors are ``sha256`` and album cursors are ``id``: both are stable,
totally ordered keys, so keyset pagination survives the loss of the v1 serial
``images.id``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from sqlalchemy import Select, select

from ..db import get_db
from ..deps import require_auth_ctx
from ..models import Album, AlbumTag, Asset, AssetSuppression, AssetTag, Tag
from ..schemas import (
    AlbumSearchResponse,
    AlbumSearchResult,
    AssetSearchResponse,
    AssetSearchResult,
)
from ..search import build_text_filter

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..policy import AuthCtx

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/assets", response_model=AssetSearchResponse)
def search_assets(
    query: str | None = None,
    tags: str | None = None,
    limit: int = 50,
    after: str | None = None,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> AssetSearchResponse:
    limit = max(1, min(limit, 100))
    # A suppressed asset is not merely unrenderable, it is unfindable.
    live_suppression = select(AssetSuppression.asset_sha256).where(
        AssetSuppression.asset_sha256 == Asset.sha256, AssetSuppression.lifted_at.is_(None)
    )
    stmt: Select[tuple[Asset]] = select(Asset).where(~live_suppression.exists())
    if query:
        stmt = stmt.where(build_text_filter(db, query, Asset.mime, Asset.storage_key))
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            stmt = (
                stmt.join(AssetTag, AssetTag.asset_sha256 == Asset.sha256, isouter=True)
                .join(Tag, Tag.id == AssetTag.tag_id, isouter=True)
                .where(Tag.name.in_(tag_list))
            )
    if after is not None:
        stmt = stmt.where(Asset.sha256 > after)
    stmt = stmt.order_by(Asset.sha256).limit(limit + 1)
    rows = db.execute(stmt).scalars().all()
    has_next = len(rows) > limit
    results = rows[:limit]
    next_cursor = results[-1].sha256 if has_next else None
    return AssetSearchResponse(
        results=[AssetSearchResult(sha256=r.sha256, mime=r.mime) for r in results],
        next_cursor=next_cursor,
    )


@router.get("/albums", response_model=AlbumSearchResponse)
def search_albums(
    query: str | None = None,
    tags: str | None = None,
    limit: int = 50,
    after: str | None = None,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> AlbumSearchResponse:
    limit = max(1, min(limit, 100))
    stmt: Select[tuple[Album]] = select(Album).where(Album.deleted_at.is_(None))
    if query:
        stmt = stmt.where(build_text_filter(db, query, Album.title, Album.description))
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            stmt = (
                stmt.join(AlbumTag, AlbumTag.album_id == Album.id, isouter=True)
                .join(Tag, Tag.id == AlbumTag.tag_id, isouter=True)
                .where(Tag.name.in_(tag_list))
            )
    if after is not None:
        stmt = stmt.where(Album.id > after)
    stmt = stmt.order_by(Album.id).limit(limit + 1)
    rows = db.execute(stmt).scalars().all()
    has_next = len(rows) > limit
    results = rows[:limit]
    next_cursor = results[-1].id if has_next else None
    return AlbumSearchResponse(
        results=[AlbumSearchResult(id=r.id, title=r.title) for r in results],
        next_cursor=next_cursor,
    )
