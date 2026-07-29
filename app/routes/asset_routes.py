"""Asset routes.

``images.id`` is gone.  The content address IS the key, so every path here is
``/assets/{sha256}``.  Two shapes replace the old ``ImageVersion``:

* a *technical* re-encode of bytes we are already rehosting is a
  :class:`~app.models.AssetRendition` — a cache, purged with its base;
* an *expressive* edit is a :class:`~app.models.Presentation` — render layers,
  no new bytes, with a per-layer kill switch.

Both go through :mod:`app.rights`.  ``/presentations`` in particular calls
:func:`app.rights.create_presentation`, which will not create a layer over an
asset whose gate is shut.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from ..config import get_settings
from ..db import get_db
from ..deps import require_auth_ctx
from ..models import (
    Asset,
    AssetOrigin,
    AssetRendition,
    Presentation,
    UserAssetLink,
)
from ..rights import (
    RightsError,
    assert_technical_transform,
    create_presentation,
    derive_state,
    disable_presentation,
    renderable_presentations,
    suppress_asset,
)
from ..s3 import presign_post_for_upload
from ..schemas import (
    AssetDetailResponse,
    CompleteUploadRequest,
    CompleteUploadResponse,
    CreatePresentationRequest,
    CreatePresentationResponse,
    CreateRenditionRequest,
    CreateRenditionResponse,
    DeriveStateResponse,
    DisablePresentationRequest,
    InitiateUploadRequest,
    OkResponse,
    OriginSummary,
    PresentationSummary,
    SetContentRatingRequest,
    SetVisibilityRequest,
    SuppressAssetRequest,
)
from ..workers.tasks import enqueue_rendition

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..policy import AuthCtx

router = APIRouter(prefix="/assets", tags=["assets"])


def _actor(ctx: AuthCtx) -> str:
    return ctx.subject


def _get_asset(db: Session, sha256: str) -> Asset:
    asset = db.get(Asset, sha256)
    if asset is None:
        raise HTTPException(status_code=404, detail="not found")
    return asset


def _derive_state_response(db: Session, sha256: str) -> DeriveStateResponse:
    state = derive_state(db, sha256)
    return DeriveStateResponse(
        sha256=state.sha256,
        permission_ok=state.permission_ok,
        bytes_ok=state.bytes_ok,
        not_suppressed=state.not_suppressed,
        derive_ok=state.derive_ok,
        reasons=list(state.reasons),
    )


@router.post("/initiate-upload")
def initiate_upload(
    payload: InitiateUploadRequest,
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> dict[str, object]:
    settings = get_settings()
    staging_key = f"uploads/{uuid.uuid4()}/{payload.filename}"
    presigned = presign_post_for_upload(staging_key, content_type=payload.mime, size=payload.size)
    presigned["staging_key"] = staging_key
    presigned["bucket"] = settings.s3_bucket
    return presigned


@router.post("/complete", response_model=CompleteUploadResponse)
def complete_upload(
    payload: CompleteUploadRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> CompleteUploadResponse:
    settings = get_settings()

    # Content-addressed: the same press shot redistributed by six retailers is
    # ONE row.  Dedup is free and automatic.
    asset = db.get(Asset, payload.sha256)
    created = False
    if asset is None:
        asset = Asset(sha256=payload.sha256, mime=payload.mime, bytes=payload.size)
        db.add(asset)
        db.flush()
        created = True

    # An asset with no origin is audit-invisible, so record one on every
    # upload.  An interactive upload is first-party own work by default; a
    # service token has to say what it really is.
    origin_in = payload.origin
    if origin_in is None and not ctx.is_service:
        origin_in = None
        db.add(
            AssetOrigin(
                asset_sha256=asset.sha256,
                source_class="user_photo",
                rights_basis="own_work",
                derive_permitted=True,
                rights_asserted_by=ctx.subject,
                asserted_at=dt.datetime.now(dt.UTC),
                fetched_at=dt.datetime.now(dt.UTC),
            )
        )
    elif origin_in is not None:
        db.add(
            AssetOrigin(
                asset_sha256=asset.sha256,
                source_id=origin_in.source_id,
                source_url=origin_in.source_url,
                capture_id=origin_in.capture_id,
                source_class=origin_in.source_class,
                rights_basis=origin_in.rights_basis,
                derive_permitted=origin_in.derive_permitted,
                permission_ref=origin_in.permission_ref,
                rights_asserted_by=origin_in.rights_asserted_by or ctx.subject,
                asserted_at=dt.datetime.now(dt.UTC),
                fetched_at=origin_in.fetched_at or dt.datetime.now(dt.UTC),
            )
        )

    # Ownership on grants, dedup on content.
    if not ctx.is_service and ctx.subject and "-" in ctx.subject:
        link = db.get(UserAssetLink, {"user_id": ctx.subject, "asset_sha256": asset.sha256})
        if not link:
            db.add(
                UserAssetLink(
                    user_id=ctx.subject,
                    tenant_id=ctx.tenant_id,
                    asset_sha256=asset.sha256,
                    role="owner",
                )
            )

    db.commit()

    try:
        from ..workers.tasks import enqueue_verify

        enqueue_verify(
            sha256=asset.sha256,
            bucket=settings.s3_bucket,
            key=payload.key,
            expected_sha256=payload.sha256,
        )
    except Exception:  # pragma: no cover - broker absence must not fail the write
        pass

    return CompleteUploadResponse(sha256=asset.sha256, created=created)


@router.get("/{sha256}", response_model=AssetDetailResponse)
def get_asset(
    sha256: str,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> AssetDetailResponse:
    asset = _get_asset(db, sha256)
    origins = (
        db.execute(select(AssetOrigin).where(AssetOrigin.asset_sha256 == sha256)).scalars().all()
    )
    layers = renderable_presentations(db, sha256)
    return AssetDetailResponse(
        sha256=asset.sha256,
        mime=asset.mime,
        bytes=asset.bytes,
        width=asset.width,
        height=asset.height,
        storage_key=asset.storage_key,
        watermark_state=asset.watermark_state,
        cmi_present=asset.cmi_present,
        content_rating=asset.content_rating,
        derived_from=asset.derived_from,
        derive_state=_derive_state_response(db, sha256),
        origins=[
            OriginSummary(
                id=o.id,
                source_id=o.source_id,
                source_url=o.source_url,
                source_class=o.source_class,
                rights_basis=o.rights_basis,
                derive_permitted=o.derive_permitted,
                permission_ref=o.permission_ref,
            )
            for o in origins
        ],
        presentations=[
            PresentationSummary(
                id=p.id,
                render_context=p.render_context,
                layer_type=p.layer_type,
                layer_asset_sha256=p.layer_asset_sha256,
                z_index=p.z_index,
                transform=p.transform,
                composite_op=p.composite_op,
                produced_by=p.produced_by,
            )
            for p in layers
        ],
    )


@router.get("/{sha256}/derive-state", response_model=DeriveStateResponse)
def get_derive_state(
    sha256: str,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> DeriveStateResponse:
    """Why the gate is open or shut.  Makes a refusal actionable."""
    _get_asset(db, sha256)
    return _derive_state_response(db, sha256)


# ---------------------------------------------------------------------------
# Renditions — technical cache
# ---------------------------------------------------------------------------


def _transform_hash(transform: dict[str, object]) -> str:
    canonical = json.dumps(transform, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@router.post("/{sha256}/renditions", response_model=CreateRenditionResponse)
def create_rendition(
    sha256: str,
    payload: CreateRenditionRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> CreateRenditionResponse:
    """A resize is the SAME expression at a different delivery size.

    No rights check is needed to re-encode bytes we are already rehosting, but
    an *expressive* key here would make the cache a derivative-work factory,
    so the transform is screened.
    """
    asset = _get_asset(db, sha256)
    try:
        assert_technical_transform(payload.transform)
    except RightsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    thash = _transform_hash(payload.transform)
    existing = db.get(AssetRendition, (sha256, thash))
    if existing is not None:
        return CreateRenditionResponse(
            sha256=sha256, transform_hash=thash, storage_key=existing.storage_key
        )

    dest_key = f"rendition/{sha256[:2]}/{sha256}/{thash}"
    db.add(
        AssetRendition(
            base_asset_sha256=sha256,
            transform_hash=thash,
            transform=payload.transform,
            mime=asset.mime,
            storage_key=dest_key,
        )
    )
    db.commit()
    enqueue_rendition(
        sha256=sha256, transform_hash=thash, transform=payload.transform, dest_key=dest_key
    )
    return CreateRenditionResponse(sha256=sha256, transform_hash=thash, storage_key=dest_key)


# ---------------------------------------------------------------------------
# Presentations — expressive edits, as data
# ---------------------------------------------------------------------------


@router.post("/{sha256}/presentations", response_model=CreatePresentationResponse)
def add_presentation(
    sha256: str,
    payload: CreatePresentationRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> CreatePresentationResponse:
    _get_asset(db, sha256)
    try:
        presentation = create_presentation(
            db,
            base_asset_sha256=sha256,
            layer_type=payload.layer_type,
            produced_by=payload.produced_by,
            actor=_actor(ctx),
            render_context=payload.render_context,
            layer_asset_sha256=payload.layer_asset_sha256,
            z_index=payload.z_index,
            transform=payload.transform,
            composite_op=payload.composite_op,
        )
    except RightsError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    db.commit()
    return CreatePresentationResponse(id=presentation.id)


@router.post("/{sha256}/presentations/{presentation_id}/disable", response_model=OkResponse)
def kill_presentation(
    sha256: str,
    presentation_id: str,
    payload: DisablePresentationRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> OkResponse:
    """The kill switch.  Dated disablement, never an erase."""
    presentation = db.get(Presentation, presentation_id)
    if presentation is None or presentation.base_asset_sha256 != sha256:
        raise HTTPException(status_code=404, detail="not found")
    if not presentation.enabled:
        return OkResponse(ok=True)
    disable_presentation(db, presentation, reason=payload.reason, actor=_actor(ctx))
    db.commit()
    return OkResponse(ok=True)


# ---------------------------------------------------------------------------
# Byte facts and takedown
# ---------------------------------------------------------------------------


@router.post("/{sha256}/content-rating", response_model=OkResponse)
def set_content_rating(
    sha256: str,
    payload: SetContentRatingRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> OkResponse:
    asset = _get_asset(db, sha256)
    asset.content_rating = payload.content_rating
    db.commit()
    return OkResponse(ok=True)


@router.post("/{sha256}/visibility", response_model=OkResponse)
def set_visibility(
    sha256: str,
    payload: SetVisibilityRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> OkResponse:
    """Visibility is a property of the GRANT, not of a rendering."""
    _get_asset(db, sha256)
    link = db.get(UserAssetLink, {"user_id": ctx.subject, "asset_sha256": sha256})
    if link is None:
        raise HTTPException(status_code=404, detail="not found")
    link.visibility = payload.visibility
    db.commit()
    return OkResponse(ok=True)


@router.post("/{sha256}/suppress", response_model=OkResponse)
def suppress(
    sha256: str,
    payload: SuppressAssetRequest,
    db: Session = Depends(get_db),  # noqa: B008
    ctx: AuthCtx = Depends(require_auth_ctx),  # noqa: B008
) -> OkResponse:
    """Takedown.  A veto that ingest can never re-derive."""
    _get_asset(db, sha256)
    suppress_asset(
        db, sha256, reason=payload.reason, actor=_actor(ctx), notice_ref=payload.notice_ref
    )
    db.commit()
    return OkResponse(ok=True)
