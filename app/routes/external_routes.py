"""External-reference routes.

``external_ref.version_id`` is dropped with ``ImageVersion``: a reference names
the bytes, and how those bytes are *rendered* is a presentation decision made
at serve time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from ..db import get_db
from ..deps import require_auth_ctx
from ..models import Asset, ExternalRef, UserAssetLink
from ..policy import AuthCtx, can_view
from ..rights import derive_state
from ..s3 import presign_get
from ..schemas import (
    CreateExternalRefRequest,
    CreateExternalRefResponse,
    ExternalAssetResponse,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

router = APIRouter(prefix="/external", tags=["external"])


@router.post("/refs", response_model=CreateExternalRefResponse)
def create_external_ref(
    payload: CreateExternalRefRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> CreateExternalRefResponse:
    if db.get(Asset, payload.asset_sha256) is None:
        raise HTTPException(status_code=404, detail="asset not found")
    ref = ExternalRef(
        ref_type=payload.ref_type,
        ref_id=payload.ref_id,
        asset_sha256=payload.asset_sha256,
        tenant_id=ctx.tenant_id,
    )
    db.add(ref)
    db.commit()
    return CreateExternalRefResponse(id=ref.id)


@router.get("/assets/by-external-ref", response_model=ExternalAssetResponse)
def by_external_ref(
    ref_type: str,
    ref_id: str,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> ExternalAssetResponse:
    ref = db.execute(
        select(ExternalRef).where(ExternalRef.ref_type == ref_type, ExternalRef.ref_id == ref_id)
    ).scalar_one_or_none()
    if not ref:
        raise HTTPException(status_code=404, detail="not found")
    asset = db.get(Asset, ref.asset_sha256)
    if asset is None:
        raise HTTPException(status_code=404, detail="not found")
    if not derive_state(db, asset.sha256).not_suppressed:
        raise HTTPException(status_code=404, detail="not found")

    link = (
        db.execute(select(UserAssetLink).where(UserAssetLink.asset_sha256 == asset.sha256))
        .scalars()
        .first()
    )
    visibility = link.visibility if link else "private"
    owner_tenant = link.tenant_id if link else None
    if not can_view(ctx, visibility, owner_tenant, asset.content_rating):
        raise HTTPException(status_code=403, detail="forbidden")

    return ExternalAssetResponse(
        sha256=asset.sha256,
        mime=asset.mime,
        width=asset.width,
        height=asset.height,
        url=presign_get(asset.storage_key),
    )
