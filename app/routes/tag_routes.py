"""Tag routes.  Asset tags key on ``asset.sha256``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ..db import get_db
from ..deps import require_auth_ctx
from ..models import AlbumTag, AssetTag, Tag
from ..schemas import (
    CreateTagRequest,
    CreateTagResponse,
    TagItemsRequest,
    TagItemsResponse,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..policy import AuthCtx

router = APIRouter(prefix="/tags", tags=["tags"])


def _resolve_tag_ids(db: Session, payload: TagItemsRequest) -> list[str]:
    ids: list[str] = list(payload.tag_ids)
    if payload.names:
        lowered = [n.lower() for n in payload.names]
        tags = db.execute(select(Tag).where(func.lower(Tag.name).in_(lowered))).scalars().all()
        ids.extend(t.id for t in tags)
    return list(dict.fromkeys(ids))


@router.post("", response_model=CreateTagResponse)
def create_tag(
    payload: CreateTagRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> CreateTagResponse:
    tag = Tag(name=payload.name, scope=payload.scope, tenant_id=payload.tenant_id)
    db.add(tag)
    db.commit()
    return CreateTagResponse(id=tag.id, name=tag.name)


@router.post("/assets/{sha256}", response_model=TagItemsResponse)
def tag_asset(
    sha256: str,
    payload: TagItemsRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> TagItemsResponse:
    ids = _resolve_tag_ids(db, payload)
    for tag_id in ids:
        existing = db.execute(
            select(AssetTag).where(AssetTag.asset_sha256 == sha256, AssetTag.tag_id == tag_id)
        ).scalar_one_or_none()
        if not existing:
            db.add(AssetTag(asset_sha256=sha256, tag_id=tag_id))
    db.commit()
    return TagItemsResponse(count=len(ids))


@router.post("/albums/{album_id}", response_model=TagItemsResponse)
def tag_album(
    album_id: str,
    payload: TagItemsRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> TagItemsResponse:
    ids = _resolve_tag_ids(db, payload)
    for tag_id in ids:
        existing = db.execute(
            select(AlbumTag).where(AlbumTag.album_id == album_id, AlbumTag.tag_id == tag_id)
        ).scalar_one_or_none()
        if not existing:
            db.add(AlbumTag(album_id=album_id, tag_id=tag_id))
    db.commit()
    return TagItemsResponse(count=len(ids))
