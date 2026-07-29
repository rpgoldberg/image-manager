"""Serve routes.

Keys are ``asset.sha256``.  Three things can come back:

* the base bytes, presigned;
* a :class:`~app.models.AssetRendition` — a technical re-encode, vetoed by
  suppression exactly as the base is;
* a render *manifest* — the presentation layers, composited client-side.  The
  manifest comes from :func:`app.rights.renderable_presentations`, never from
  a direct query, so a shut gate returns no layers rather than layers a client
  is trusted not to draw.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from ..db import get_db
from ..deps import get_auth_ctx, require_auth_ctx
from ..models import Asset, AssetRendition, UserAssetLink
from ..policy import AuthCtx, can_view
from ..rights import derive_state, renderable_presentations
from ..s3 import presign_get

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

router = APIRouter(tags=["serve"])


def _asset_or_404(db: Session, sha256: str) -> Asset:
    asset = db.get(Asset, sha256)
    if asset is None:
        raise HTTPException(status_code=404, detail="not found")
    return asset


def _effective_visibility(db: Session, sha256: str, ctx: AuthCtx | None) -> tuple[str, str | None]:
    """Visibility now lives on the grant.  The caller's own grant wins; if the
    caller has none, the most permissive grant on these bytes decides whether
    they are visible at all."""
    if ctx is not None:
        own = db.get(UserAssetLink, {"user_id": ctx.subject, "asset_sha256": sha256})
        if own is not None:
            return own.visibility, own.tenant_id
    links = (
        db.execute(select(UserAssetLink).where(UserAssetLink.asset_sha256 == sha256))
        .scalars()
        .all()
    )
    order = {"public": 0, "catalog": 1, "tenant": 2, "private": 3}
    if not links:
        return "private", None
    best = min(links, key=lambda link: order.get(link.visibility, 3))
    return best.visibility, best.tenant_id


def _suppressed(db: Session, sha256: str) -> bool:
    return not derive_state(db, sha256).not_suppressed


@router.get("/serve/{sha256}")
def serve_asset(
    sha256: str,
    rendition: str | None = None,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx | None = Depends(get_auth_ctx),  # noqa: B008
) -> Response:
    asset = _asset_or_404(db, sha256)
    if _suppressed(db, sha256):
        raise HTTPException(status_code=404, detail="not found")

    visibility, owner_tenant = _effective_visibility(db, sha256, ctx)
    if visibility == "private" and ctx is None:
        raise HTTPException(status_code=403, detail="forbidden")
    if not can_view(ctx, visibility, owner_tenant, asset.content_rating):
        raise HTTPException(status_code=403, detail="forbidden")

    storage_key = asset.storage_key
    if rendition:
        row = db.get(AssetRendition, (sha256, rendition))
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        storage_key = row.storage_key

    resp = Response(status_code=302)
    resp.headers["Location"] = presign_get(storage_key)
    resp.headers["Cache-Control"] = "private, max-age=600"
    resp.headers["ETag"] = asset.sha256
    return resp


@router.get("/public/{sha256}")
def public_serve(
    sha256: str,
    db: Session = Depends(get_db),  # noqa: B008
) -> Response:
    asset = _asset_or_404(db, sha256)
    if _suppressed(db, sha256):
        raise HTTPException(status_code=404, detail="not found")
    visibility, _ = _effective_visibility(db, sha256, None)
    if visibility != "public":
        raise HTTPException(status_code=403, detail="forbidden")

    resp = Response(status_code=302)
    resp.headers["Location"] = presign_get(asset.storage_key)
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["ETag"] = asset.sha256
    return resp


@router.get("/render/{sha256}")
def render_manifest(
    sha256: str,
    render_context: str = "default",
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> dict[str, object]:
    """The declarative composite, applied client-side.

    An empty ``layers`` list is the correct answer for a shut gate: the base
    bytes are still servable (we are rehosting them), but nothing may be
    composited on top.
    """
    asset = _asset_or_404(db, sha256)
    visibility, owner_tenant = _effective_visibility(db, sha256, ctx)
    if not can_view(ctx, visibility, owner_tenant, asset.content_rating):
        raise HTTPException(status_code=403, detail="forbidden")
    if _suppressed(db, sha256):
        raise HTTPException(status_code=404, detail="not found")

    layers = renderable_presentations(db, sha256, render_context)
    return {
        "sha256": sha256,
        "render_context": render_context,
        "base_url": presign_get(asset.storage_key),
        "layers": [
            {
                "layer_type": layer.layer_type,
                "layer_url": (
                    presign_get(
                        db.get(Asset, layer.layer_asset_sha256).storage_key  # type: ignore[union-attr]
                    )
                    if layer.layer_asset_sha256
                    else None
                ),
                "z_index": layer.z_index,
                "transform": layer.transform,
                "composite_op": layer.composite_op,
                "produced_by": layer.produced_by,
            }
            for layer in layers
        ],
    }
