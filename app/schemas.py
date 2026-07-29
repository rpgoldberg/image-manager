from __future__ import annotations

# ruff: noqa: TCH003 - `dt` must stay a RUNTIME import.  Pydantic resolves field
# annotations when it builds the model, so moving datetime into a TYPE_CHECKING
# block leaves every `dt.datetime` field unresolvable and every request body
# containing one fails to validate.  Verified: doing so breaks 18 tests.
# FastAPI's `Depends(...)` parameters are different — those annotations are
# never evaluated, which is why the route modules can and do defer theirs.
import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

#: Keys are ``asset.sha256`` now, not ``images.id``.  The wire type carries the
#: same ``^[0-9a-f]{64}$`` rule the sha256_hex domain does, so a malformed
#: digest is a 422 at the edge rather than a 500 in the ORM.
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

Visibility = Literal["private", "tenant", "public", "catalog"]
ContentRating = Literal["all_ages", "teen", "adult", "unknown"]
SourceClass = Literal[
    "manufacturer_press", "retailer_studio", "user_photo", "derived_own", "unknown"
]
RightsBasis = Literal[
    "user_licence", "permission_granted", "unlicensed_norm", "own_work", "unknown"
]
DepictionRole = Literal["main", "box", "detail", "scale_ref", "user_shelf", "comparison"]
LayerType = Literal["matte_mask", "occluder", "depth_transform", "watermark"]
RenderContext = Literal["default", "case_shelf", "detail"]
CompositeOp = Literal["source-over", "destination-in", "destination-out", "multiply", "screen"]
Confidence = Literal["high", "medium", "low", "unknown"]

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class DevTokenRequest(BaseModel):
    user_id: str | None = None
    tenant_id: str | None = None
    aud: str | None = Field(default=None, description="service:<name> for service tokens")


class DevTokenResponse(BaseModel):
    token: str


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


class InitiateUploadRequest(BaseModel):
    filename: str
    mime: str
    size: int = Field(gt=0)


class InitiateUploadResponse(BaseModel):
    model_config = {"extra": "allow"}

    url: str
    fields: dict[str, str]
    staging_key: str
    bucket: str


class OriginRequest(BaseModel):
    """How we came to hold these bytes.

    ``derive_permitted`` defaults FALSE and cannot be set without an
    affirmative ``rights_basis``; ``permission_granted`` must name the
    instrument that granted it.
    """

    source_id: str | None = None
    source_url: str | None = None
    capture_id: str | None = None
    source_class: SourceClass = "unknown"
    rights_basis: RightsBasis = "unknown"
    derive_permitted: bool = False
    permission_ref: str | None = None
    rights_asserted_by: str | None = None
    fetched_at: dt.datetime | None = None


class CompleteUploadRequest(BaseModel):
    sha256: Sha256
    key: str
    mime: str
    size: int = Field(gt=0)
    #: An asset with no origin is audit-invisible.  Uploads default to a
    #: first-party own-work origin; scrapers must say what they really are.
    origin: OriginRequest | None = None


class CompleteUploadResponse(BaseModel):
    sha256: str
    created: bool


class OriginSummary(BaseModel):
    id: str
    source_id: str | None = None
    source_url: str | None = None
    source_class: str
    rights_basis: str
    derive_permitted: bool
    permission_ref: str | None = None


class PresentationSummary(BaseModel):
    id: str
    render_context: str
    layer_type: str
    layer_asset_sha256: str | None = None
    z_index: int
    transform: dict[str, Any]
    composite_op: str
    produced_by: str


class DeriveStateResponse(BaseModel):
    """The gate, made legible.  A refusal says which half closed."""

    sha256: str
    permission_ok: bool
    bytes_ok: bool
    not_suppressed: bool
    derive_ok: bool
    reasons: list[str] = []


class AssetDetailResponse(BaseModel):
    sha256: str
    mime: str
    bytes: int | None = None
    width: int | None = None
    height: int | None = None
    storage_key: str | None = None
    watermark_state: str
    cmi_present: bool | None = None
    content_rating: str
    derived_from: str | None = None
    derive_state: DeriveStateResponse
    origins: list[OriginSummary] = []
    presentations: list[PresentationSummary] = []


class SetContentRatingRequest(BaseModel):
    content_rating: ContentRating


class SetVisibilityRequest(BaseModel):
    visibility: Visibility


class OkResponse(BaseModel):
    ok: bool


# ---------------------------------------------------------------------------
# Renditions — a technical CACHE, never an expressive edit
# ---------------------------------------------------------------------------


class CreateRenditionRequest(BaseModel):
    transform: dict[str, Any] = Field(default_factory=dict)


class CreateRenditionResponse(BaseModel):
    sha256: str
    transform_hash: str
    storage_key: str


# ---------------------------------------------------------------------------
# Presentations — render layers, with a per-layer kill switch
# ---------------------------------------------------------------------------


class CreatePresentationRequest(BaseModel):
    layer_type: LayerType
    produced_by: str
    render_context: RenderContext = "default"
    layer_asset_sha256: Sha256 | None = None
    z_index: int = 0
    transform: dict[str, Any] = Field(default_factory=dict)
    composite_op: CompositeOp = "source-over"


class CreatePresentationResponse(BaseModel):
    id: str


class DisablePresentationRequest(BaseModel):
    reason: str = Field(min_length=1)


class SuppressAssetRequest(BaseModel):
    reason: str = Field(min_length=1)
    notice_ref: str | None = None


# ---------------------------------------------------------------------------
# Depictions — sourced CLAIMS, not foreign keys
# ---------------------------------------------------------------------------


class CreateDepictionRequest(BaseModel):
    role: DepictionRole
    source_id: str | None = None
    source_native_id: str | None = None
    subject_product_id: str | None = None
    product_id: str | None = None
    alt_text: str | None = None
    source_url: str | None = None
    ruleset_version: str | None = None
    conf: Confidence = "unknown"
    rank: int | None = None
    as_of: dt.datetime | None = None


class CreateDepictionResponse(BaseModel):
    id: str


class DepictionSummary(BaseModel):
    id: str
    asset_sha256: str
    role: str
    source_id: str | None = None
    source_native_id: str | None = None
    subject_product_id: str | None = None
    product_id: str | None = None
    conf: str
    rank: int | None = None


class DepictionListResponse(BaseModel):
    results: list[DepictionSummary] = []


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------


class CreateAlbumRequest(BaseModel):
    title: str
    description: str | None = None
    default_visibility: Literal["private", "tenant", "public"] = "private"
    is_shareable: bool = False
    share_age_threshold: ContentRating = "all_ages"


class CreateAlbumResponse(BaseModel):
    id: str


class UpdateAlbumRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    default_visibility: Literal["private", "tenant", "public"] | None = None
    is_shareable: bool | None = None
    share_age_threshold: ContentRating | None = None


class AddAlbumItemRequest(BaseModel):
    asset_sha256: Sha256
    position: int | None = None


class AddAlbumItemResponse(BaseModel):
    position: int


class ReorderItem(BaseModel):
    from_position: int
    to_position: int


class ReorderRequest(BaseModel):
    items: list[ReorderItem] = []


class AlbumItemSummary(BaseModel):
    position: int
    asset_sha256: str
    item_visibility: str | None = None


class AlbumDetailResponse(BaseModel):
    id: str
    title: str
    description: str | None = None
    default_visibility: str
    is_shareable: bool
    share_age_threshold: str
    items: list[AlbumItemSummary] = []


class ShareAlbumRequest(BaseModel):
    enable: bool
    share_age_threshold: ContentRating | None = None


class ShareAlbumResponse(BaseModel):
    share_url: str | None = None


class AlbumCoverResponse(BaseModel):
    storage_key: str


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class CreateTagRequest(BaseModel):
    name: str
    scope: Literal["global", "tenant", "user"]
    tenant_id: str | None = None


class CreateTagResponse(BaseModel):
    id: str
    name: str


class TagItemsRequest(BaseModel):
    tag_ids: list[str] = []
    names: list[str] = []


class TagItemsResponse(BaseModel):
    count: int


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class AssetSearchResult(BaseModel):
    sha256: str
    mime: str | None = None


class AssetSearchResponse(BaseModel):
    results: list[AssetSearchResult]
    next_cursor: str | None = None


class AlbumSearchResult(BaseModel):
    id: str
    title: str


class AlbumSearchResponse(BaseModel):
    results: list[AlbumSearchResult]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# External refs
# ---------------------------------------------------------------------------


class CreateExternalRefRequest(BaseModel):
    ref_type: str
    ref_id: str
    asset_sha256: Sha256


class CreateExternalRefResponse(BaseModel):
    id: str


class ExternalAssetResponse(BaseModel):
    sha256: str
    mime: str | None = None
    width: int | None = None
    height: int | None = None
    url: str
