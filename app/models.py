"""Media schema v3 — ORM mapping.

Source of truth for the DDL is ``migrations/versions/0002_media_schema_v3.py``,
which executes the adjudicated schema verbatim.  This module maps onto it.

Two deliberate divergences, both because the test suite runs on SQLite:

* ``sha256_hex`` is a PostgreSQL ``DOMAIN``.  Here it is :class:`Sha256Hex`, a
  ``TypeDecorator`` that performs the *same* ``^[0-9a-f]{64}$`` validation on
  the bind parameter, so the check holds on every dialect rather than only on
  PostgreSQL.
* ``gen_random_uuid()`` and trigger-supplied defaults are expressed
  Python-side.  The PostgreSQL server defaults and triggers still exist; they
  come from the migration.

The rules that gate derivation (§1202 exposure) are NOT left to the database.
They are enforced in :mod:`app.rights`, which every write and serve path calls.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "media"

# Mirrors the spine's enums (0001_aggregation_base.sql:48,50) and v1's
# visibility CHECK.  Names and member order are kept identical to the DDL.
CONFIDENCE_VALUES = ("high", "medium", "low", "unknown")
CONTENT_RATING_VALUES = ("all_ages", "teen", "adult", "unknown")
VISIBILITY_VALUES = ("private", "tenant", "public", "catalog")

SOURCE_CLASS_VALUES = (
    "manufacturer_press",
    "retailer_studio",
    "user_photo",
    "derived_own",
    "unknown",
)
RIGHTS_BASIS_VALUES = (
    "user_licence",
    "permission_granted",
    "unlicensed_norm",
    "own_work",
    "unknown",
)
DEPICTION_ROLE_VALUES = ("main", "box", "detail", "scale_ref", "user_shelf", "comparison")
LAYER_TYPE_VALUES = ("matte_mask", "occluder", "depth_transform", "watermark")
RENDER_CONTEXT_VALUES = ("default", "case_shelf", "detail")
COMPOSITE_OP_VALUES = ("source-over", "destination-in", "destination-out", "multiply", "screen")
WATERMARK_STATE_VALUES = ("clean", "watermarked", "unchecked")
JOB_STATE_VALUES = ("queued", "running", "succeeded", "failed", "skipped_rights")
TAG_SCOPE_VALUES = ("global", "tenant", "user")
PRESENTATION_EVENT_VALUES = ("created", "disabled", "re_enabled")

# 'unknown' must be the MOST restrictive for age gating, which the enum's own
# ordinal position is not.  Mirrors the DDL's rating_rank() function.
RATING_RANK: dict[str, int] = {"all_ages": 0, "teen": 1, "adult": 2, "unknown": 3}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def rating_rank(rating: str | None) -> int:
    """Explicit age-gate ordering.  Unknown ranks most restrictive."""
    return RATING_RANK.get(rating or "unknown", 3)


class Sha256Hex(sa.types.TypeDecorator[str]):
    """The ``sha256_hex`` domain, enforced on every dialect.

    ``CHAR(64)`` validated nothing: ``'deadbeef'``, ``'../../etc/passwd'`` and
    an UPPERCASE duplicate of the same content were all accepted as primary
    keys.  Rejecting at bind time gives SQLite the same guarantee the
    PostgreSQL domain gives.
    """

    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: sa.Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _SHA256_RE.match(value):
            raise ValueError(f"not a lowercase hex sha256 digest: {value!r}")
        return value


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _enum(values: tuple[str, ...], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, schema=SCHEMA, validate_strings=True)


class Base(DeclarativeBase):
    metadata = sa.MetaData(schema=SCHEMA)


# ---------------------------------------------------------------------------
# 1. asset — immutable bytes.  No rights opinion beyond what the BYTES carry.
# ---------------------------------------------------------------------------


class Asset(Base):
    __tablename__ = "asset"

    sha256: Mapped[str] = mapped_column(Sha256Hex, primary_key=True)
    mime: Mapped[str] = mapped_column(sa.Text, nullable=False)
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    bytes: Mapped[int | None] = mapped_column(sa.BigInteger)

    # BYTE FACTS.  One sha256 = one pixel buffer = one truth.  These lived on
    # asset_origin in v2, where two origins of the same bytes contradicted each
    # other and the more permissive one opened the section 1202 gate.
    watermark_state: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="unchecked", server_default="unchecked"
    )
    cmi_present: Mapped[bool | None] = mapped_column(sa.Boolean)
    # WHAT the IPTC/XMP rights fields said.  Section 1202 requires knowing the
    # notice, not merely that one existed.
    cmi: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)
    detector_version: Mapped[str | None] = mapped_column(sa.Text)
    phash: Mapped[str | None] = mapped_column(sa.Text)
    content_rating: Mapped[str] = mapped_column(
        _enum(CONTENT_RATING_VALUES, "content_rating"),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )

    # DERIVATION.  Single pointer, depth clamped to 1 by the composite FK below.
    derived_from: Mapped[str | None] = mapped_column(Sha256Hex)
    derived_under_basis: Mapped[str | None] = mapped_column(sa.Text)
    derive_ok_latched: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )

    # The content address IS the location.  v2's free-form storage_key is what
    # made the tombstone hack work on a table documented "never mutated".
    storage_key: Mapped[str] = mapped_column(
        sa.Text,
        sa.Computed(
            "'sha256/'||substr(sha256,1,2)||'/'||substr(sha256,3,2)||'/'||sha256",
            persisted=True,
        ),
    )
    derive_depth: Mapped[int] = mapped_column(
        sa.SmallInteger,
        sa.Computed("CASE WHEN derived_from IS NULL THEN 0 ELSE 1 END", persisted=True),
    )
    parent_depth: Mapped[int | None] = mapped_column(
        sa.SmallInteger,
        sa.Computed("CASE WHEN derived_from IS NULL THEN NULL ELSE 0 END", persisted=True),
    )
    parent_latch: Mapped[bool | None] = mapped_column(
        sa.Boolean,
        sa.Computed("CASE WHEN derived_from IS NULL THEN NULL ELSE true END", persisted=True),
    )
    is_derivation: Mapped[bool] = mapped_column(
        sa.Boolean, sa.Computed("derived_from IS NOT NULL", persisted=True)
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )

    __table_args__ = (
        sa.CheckConstraint(
            "watermark_state IN ('clean','watermarked','unchecked')", name="asset_watermark_state"
        ),
        sa.CheckConstraint("derived_from IS DISTINCT FROM sha256", name="asset_no_self_derive"),
        sa.CheckConstraint(
            "(derived_from IS NULL) = (derived_under_basis IS NULL)",
            name="asset_basis_iff_derived",
        ),
        # FK targets.  asset_derivation_key also makes "parent must be a
        # PERMITTED root" expressible as one FK: MATCH SIMPLE means the whole
        # constraint is skipped when derived_from IS NULL.
        sa.Index(
            "asset_derivation_key", "sha256", "derive_depth", "derive_ok_latched", unique=True
        ),
        sa.Index("asset_is_derivation_key", "sha256", "is_derivation", unique=True),
        sa.Index("asset_parentage_key", "sha256", "derived_from", unique=True),
        sa.ForeignKeyConstraint(
            ["derived_from", "parent_depth", "parent_latch"],
            [
                f"{SCHEMA}.asset.sha256",
                f"{SCHEMA}.asset.derive_depth",
                f"{SCHEMA}.asset.derive_ok_latched",
            ],
            name="parent_must_be_permitted_root",
        ),
        sa.Index(
            "asset_derived_from",
            "derived_from",
            postgresql_where=sa.text("derived_from IS NOT NULL"),
            sqlite_where=sa.text("derived_from IS NOT NULL"),
        ),
        sa.Index(
            "asset_phash",
            "phash",
            postgresql_where=sa.text("phash IS NOT NULL"),
            sqlite_where=sa.text("phash IS NOT NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# 2. source_policy — LOCAL replica of the spine's per-source image policy.
#    A fail-closed control cannot contain a network call.  STALE => CLOSED.
# ---------------------------------------------------------------------------


class SourcePolicy(Base):
    __tablename__ = "source_policy"

    source_id: Mapped[str] = mapped_column(sa.Uuid(as_uuid=False), primary_key=True)
    site: Mapped[str] = mapped_column(sa.Text, nullable=False)
    image_hotlink_ok: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    image_rehost_ok: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    image_derive_ok: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    takedown_contact: Mapped[str | None] = mapped_column(sa.Text)
    refreshed_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )


class ProductRedirect(Base):
    """Spine merge-as-redirect.  Resolved at READ with COALESCE.

    Never rewrite ``depiction.product_id``: that destroys un-merge
    reversibility.
    """

    __tablename__ = "product_redirect"

    loser_product_id: Mapped[str] = mapped_column(sa.Uuid(as_uuid=False), primary_key=True)
    winner_product_id: Mapped[str] = mapped_column(sa.Uuid(as_uuid=False), nullable=False)
    merged_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )

    __table_args__ = (
        sa.CheckConstraint("loser_product_id <> winner_product_id", name="redirect_not_self"),
    )


# ---------------------------------------------------------------------------
# 3. asset_origin — how we came to hold these bytes, and what we may do.
#    Acquisition facts ONLY.  Byte facts live on asset.
# ---------------------------------------------------------------------------


class AssetOrigin(Base):
    __tablename__ = "asset_origin"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), nullable=False
    )
    source_id: Mapped[str | None] = mapped_column(
        sa.Uuid(as_uuid=False), sa.ForeignKey(f"{SCHEMA}.source_policy.source_id")
    )
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    # SOFT reference to raw.capture, which is NOT BUILT.  Deliberately not an
    # FK; add the FK in the migration that creates raw.capture.  Kept because
    # it cannot be backfilled once the captures are gone.
    capture_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))
    fetched_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    source_class: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rights_basis: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # THE GATE.  Default DENY.
    derive_permitted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )

    # The permission's own provenance.  v2 recorded a boolean with no answer to
    # "who granted this, when, under what instrument" — the missing half of a
    # control whose purpose is making good faith demonstrable.
    permission_ref: Mapped[str | None] = mapped_column(sa.Text)
    rights_asserted_by: Mapped[str | None] = mapped_column(sa.Text)
    asserted_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    ingested_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )

    # 'derived_own' origins may only attach to an asset that HAS a parent.
    # MATCH SIMPLE: NULL for every other class => constraint skipped.
    requires_parent: Mapped[bool | None] = mapped_column(
        sa.Boolean,
        sa.Computed(
            "CASE WHEN source_class = 'derived_own' THEN true ELSE NULL END", persisted=True
        ),
    )

    __table_args__ = (
        sa.CheckConstraint(
            "source_class IN ('manufacturer_press','retailer_studio','user_photo',"
            "'derived_own','unknown')",
            name="origin_source_class",
        ),
        sa.CheckConstraint(
            "rights_basis IN ('user_licence','permission_granted','unlicensed_norm',"
            "'own_work','unknown')",
            name="origin_rights_basis",
        ),
        # v2 accepted ('unlicensed_norm', derive_permitted=TRUE).  The gate's
        # premise is that permission is AFFIRMATIVE.
        sa.CheckConstraint(
            "NOT derive_permitted OR rights_basis IN "
            "('user_licence','permission_granted','own_work')",
            name="origin_permission_needs_basis",
        ),
        sa.CheckConstraint(
            "rights_basis <> 'permission_granted' OR "
            "nullif(btrim(permission_ref),'') IS NOT NULL",
            name="origin_grant_names_instrument",
        ),
        # Our own derivation is by definition first-party; a 'derived_own'
        # origin carrying a source_id would mean we scraped our own mask.
        sa.CheckConstraint(
            "source_class <> 'derived_own' OR (source_id IS NULL AND rights_basis = 'own_work')",
            name="origin_derived_own_is_first_party",
        ),
        sa.ForeignKeyConstraint(
            ["asset_sha256", "requires_parent"],
            [f"{SCHEMA}.asset.sha256", f"{SCHEMA}.asset.is_derivation"],
            name="parent_asset_must_exist",
        ),
        # NULLS NOT DISTINCT: source_id is NULL for every user upload, so three
        # byte-identical uploads produced three origin rows carrying
        # potentially contradictory derive_permitted.
        sa.Index(
            "asset_origin_dedup",
            "asset_sha256",
            "source_id",
            "source_url",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        # The takedown screen's access path.  v2 left the one query that must
        # be fast under legal pressure as a sequential scan.
        sa.Index("asset_origin_source", "source_id"),
        sa.Index(
            "asset_origin_capture",
            "capture_id",
            postgresql_where=sa.text("capture_id IS NOT NULL"),
            sqlite_where=sa.text("capture_id IS NOT NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# 4. asset_suppression — the takedown VETO.  Not an origin.  Never re-derived.
# ---------------------------------------------------------------------------


class AssetSuppression(Base):
    __tablename__ = "asset_suppression"

    asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), primary_key=True
    )
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    notice_ref: Mapped[str | None] = mapped_column(sa.Text)
    suppressed_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )
    suppressed_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    lifted_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    lifted_by: Mapped[str | None] = mapped_column(sa.Text)
    lift_reason: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.CheckConstraint("(lifted_at IS NULL) = (lifted_by IS NULL)", name="lift_is_dated"),
        sa.CheckConstraint(
            "lifted_at IS NULL OR lifted_at >= suppressed_at", name="lift_after_suppress"
        ),
        sa.Index(
            "asset_suppression_live",
            "asset_sha256",
            postgresql_where=sa.text("lifted_at IS NULL"),
            sqlite_where=sa.text("lifted_at IS NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# 5. depiction — a sourced CLAIM that an asset shows a product.
#    Image -> product is NOT a foreign key.  Retailers demonstrably attach
#    wrong-variant photos; a FK asserts truth, a claim records who said so.
# ---------------------------------------------------------------------------


def _default_last_seen_at(context: Any) -> Any:
    """Mirror of the ``depiction_last_seen_default`` trigger.

    A column default cannot reference a sibling column (spine 0001:181-185),
    so PostgreSQL does this in a BEFORE INSERT trigger.  Python-side we can
    read the sibling out of the pending parameters.
    """
    return context.get_current_parameters()["as_of"]


class Depiction(Base):
    __tablename__ = "depiction"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), nullable=False
    )
    source_id: Mapped[str | None] = mapped_column(
        sa.Uuid(as_uuid=False), sa.ForeignKey(f"{SCHEMA}.source_policy.source_id")
    )
    # The source's own id for the LISTED ITEM (pre-ER, stable).
    source_native_id: Mapped[str | None] = mapped_column(sa.Text)
    # Set when the claim's subject is a PRODUCT rather than the listing:
    # user shelf photos, multi-variant comparison shots.
    subject_product_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))
    # ER-assigned, MUTABLE, nullable.  v2's NOT NULL blocked media ingest
    # until ER resolved.
    product_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    alt_text: Mapped[str | None] = mapped_column(sa.Text)
    # The LISTING page that asserted it, not the CDN byte URL.
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    # Purge-by-ruleset when an extractor attaches wrong photos.
    ruleset_version: Mapped[str | None] = mapped_column(sa.Text)
    conf: Mapped[str] = mapped_column(
        _enum(CONFIDENCE_VALUES, "confidence"),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    # SOURCE TIER, "lower = more authoritative".  Deliberately NOT unique:
    # two equally authoritative sources are normal.
    rank: Mapped[int | None] = mapped_column(sa.Integer)
    # FIRST ingested.  Immutable.
    as_of: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_default_last_seen_at
    )
    ingested_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )

    subject_key: Mapped[str] = mapped_column(
        sa.Text,
        sa.Computed(
            "COALESCE(source_native_id,'') || '|' || COALESCE(CAST(subject_product_id AS TEXT),'')",
            persisted=True,
        ),
    )

    __table_args__ = (
        sa.CheckConstraint(
            "role IN ('main','box','detail','scale_ref','user_shelf','comparison')",
            name="depiction_role",
        ),
        sa.CheckConstraint("last_seen_at >= as_of", name="depiction_recency_ordered"),
        # A claim with no stable subject handle cannot be deduped; refuse it
        # LOUDLY rather than silently merging every such claim into one row.
        sa.CheckConstraint(
            "source_native_id IS NOT NULL OR subject_product_id IS NOT NULL",
            name="depiction_has_subject",
        ),
        sa.CheckConstraint(
            "source_id IS NOT NULL OR subject_product_id IS NOT NULL",
            name="depiction_first_party_names_product",
        ),
        sa.Index(
            "depiction_dedup",
            "source_id",
            "subject_key",
            "asset_sha256",
            "role",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        sa.Index("depiction_product", "product_id", "role", "rank"),
        sa.Index("depiction_asset", "asset_sha256"),
        sa.Index(
            "depiction_ruleset",
            "ruleset_version",
            postgresql_where=sa.text("ruleset_version IS NOT NULL"),
            sqlite_where=sa.text("ruleset_version IS NOT NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# 6. presentation — how we render, as DATA.  Base bytes never touched.
# ---------------------------------------------------------------------------


class Presentation(Base):
    __tablename__ = "presentation"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    base_asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), nullable=False
    )
    # One asset legitimately carries a CaseShelf rotation AND a detail-page
    # transform.
    render_context: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="default", server_default="default"
    )
    layer_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    layer_asset_sha256: Mapped[str | None] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256")
    )
    z_index: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=0, server_default="0"
    )
    transform: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default="{}"
    )
    composite_op: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="source-over", server_default="source-over"
    )
    produced_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # THE KILL SWITCH.
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    disabled_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )

    # A matte mask may only be composited onto the image it was DERIVED FROM.
    mask_base: Mapped[str | None] = mapped_column(
        Sha256Hex,
        sa.Computed(
            "CASE WHEN layer_type = 'matte_mask' THEN base_asset_sha256 ELSE NULL END",
            persisted=True,
        ),
    )

    __table_args__ = (
        sa.CheckConstraint(
            "layer_type IN ('matte_mask','occluder','depth_transform','watermark')",
            name="pres_layer_type",
        ),
        sa.CheckConstraint(
            "render_context IN ('default','case_shelf','detail')", name="pres_render_context"
        ),
        # v2's one-directional CHECK accepted enabled=TRUE with disabled_at and
        # disabled_reason='DMCA takedown' still set: a failed takedown that
        # looks like a completed one in every audit query.
        sa.CheckConstraint("enabled = (disabled_at IS NULL)", name="pres_enabled_iff_no_disable"),
        sa.CheckConstraint(
            "disabled_at IS NULL OR nullif(btrim(disabled_reason),'') IS NOT NULL",
            name="pres_disable_needs_reason",
        ),
        sa.CheckConstraint(
            "disabled_at IS NULL OR disabled_at >= created_at", name="pres_disable_after_create"
        ),
        # Unconstrained free text flowing into a client-side canvas operation.
        sa.CheckConstraint(
            "composite_op IN ('source-over','destination-in','destination-out',"
            "'multiply','screen')",
            name="pres_composite_op",
        ),
        sa.CheckConstraint(
            "(layer_type = 'depth_transform') = (layer_asset_sha256 IS NULL)",
            name="pres_layer_asset_iff_not_transform",
        ),
        sa.CheckConstraint(
            "layer_asset_sha256 IS DISTINCT FROM base_asset_sha256", name="pres_no_self_composite"
        ),
        sa.ForeignKeyConstraint(
            ["layer_asset_sha256", "mask_base"],
            [f"{SCHEMA}.asset.sha256", f"{SCHEMA}.asset.derived_from"],
            name="pres_mask_derived_from_base",
        ),
        # One live layer of a kind per (base, context).
        sa.Index(
            "presentation_one_live",
            "base_asset_sha256",
            "render_context",
            "layer_type",
            unique=True,
            postgresql_where=sa.text("enabled"),
            sqlite_where=sa.text("enabled"),
        ),
        # Deterministic composite order: 3 enabled layers at the same z_index
        # made the render non-reproducible, and an occluder sorting above a
        # watermark obscures CMI.
        sa.Index(
            "presentation_z",
            "base_asset_sha256",
            "render_context",
            "z_index",
            unique=True,
            postgresql_where=sa.text("enabled"),
            sqlite_where=sa.text("enabled"),
        ),
        sa.Index(
            "presentation_base",
            "base_asset_sha256",
            "render_context",
            "z_index",
            postgresql_where=sa.text("enabled"),
            sqlite_where=sa.text("enabled"),
        ),
    )


class PresentationEvent(Base):
    """Two mutable columns cannot record a history, and "good faith
    demonstrable" IS a history.  Append-only.
    """

    __tablename__ = "presentation_event"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    presentation_id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), sa.ForeignKey(f"{SCHEMA}.presentation.id"), nullable=False
    )
    event: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.Text)
    actor: Mapped[str] = mapped_column(sa.Text, nullable=False)
    at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )

    __table_args__ = (
        sa.CheckConstraint(
            "event IN ('created','disabled','re_enabled')", name="presentation_event_event_check"
        ),
        sa.CheckConstraint(
            "event <> 'disabled' OR nullif(btrim(reason),'') IS NOT NULL",
            name="pres_event_disable_has_reason",
        ),
        sa.Index("presentation_event_pres", "presentation_id", "at"),
    )


# ---------------------------------------------------------------------------
# 7. presentation_job — in-flight work.  v2 had no vocabulary for it: an OOMed
#    BiRefNet run was indistinguishable from "never queued", and a retry minted
#    new mask bytes => a new sha256 => a duplicate live layer.
# ---------------------------------------------------------------------------


class PresentationJob(Base):
    __tablename__ = "presentation_job"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    base_asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), nullable=False
    )
    render_context: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="default", server_default="default"
    )
    layer_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    produced_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="queued", server_default="queued"
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=3, server_default="3"
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    # The S3 object written BEFORE the txn -> orphan reaper.
    staged_key: Mapped[str | None] = mapped_column(sa.Text)
    queued_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed','skipped_rights')",
            name="presentation_job_state_check",
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded','failed','skipped_rights')) = (finished_at IS NOT NULL)",
            name="job_terminal_is_dated",
        ),
        sa.CheckConstraint(
            "state <> 'failed' OR nullif(btrim(last_error),'') IS NOT NULL",
            name="job_failure_has_reason",
        ),
        sa.Index(
            "presentation_job_one_live",
            "base_asset_sha256",
            "render_context",
            "layer_type",
            unique=True,
            postgresql_where=sa.text("state IN ('queued','running')"),
            sqlite_where=sa.text("state IN ('queued','running')"),
        ),
        sa.Index("presentation_job_queue", "state", "queued_at"),
    )


# ---------------------------------------------------------------------------
# 8. asset_rendition — TECHNICAL variants (resize / transcode).  A CACHE.
#    A resize is the SAME expression at a different delivery size; given that
#    we are already rehosting the full file, its marginal exposure is ~0.
#    Matting/cropping is NOT in here — that lives in presentation.
# ---------------------------------------------------------------------------

#: Keys whose presence turns the cache into a derivative-work factory.
EXPRESSIVE_TRANSFORM_KEYS = frozenset(
    {"crop", "matte", "mask", "rotate", "skew", "remove_bg", "watermark"}
)


class AssetRendition(Base):
    __tablename__ = "asset_rendition"

    base_asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), primary_key=True
    )
    transform_hash: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    transform: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    mime: Mapped[str] = mapped_column(sa.Text, nullable=False)
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    storage_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )

    # NOTE: the DDL enforces `rendition_is_technical` with the jsonb `?|`
    # operator, which has no SQLite equivalent.  app.rights.assert_technical_
    # transform() applies the same rule on every dialect, at write time.


class BlobPurgeQueue(Base):
    __tablename__ = "blob_purge_queue"

    storage_key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    queued_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )
    purged_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))


# ---------------------------------------------------------------------------
# 9. RETAINED FROM v1, rekeyed to asset.sha256.  Every version_id column is
#    DROPPED: ImageVersion no longer exists and four tables carried a dangling
#    pointer to it.
# ---------------------------------------------------------------------------


class UserAssetLink(Base):
    """v1 ``UserImageLink``.  Dedup on content, ownership on grants."""

    __tablename__ = "user_asset_link"

    user_id: Mapped[str] = mapped_column(sa.Uuid(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))
    asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), primary_key=True
    )
    role: Mapped[str | None] = mapped_column(sa.Text)
    visibility: Mapped[str] = mapped_column(
        _enum(VISIBILITY_VALUES, "visibility"),
        nullable=False,
        default="private",
        server_default="private",
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )

    __table_args__ = (sa.Index("user_asset_visibility", "user_id", "visibility"),)


class Album(Base):
    __tablename__ = "album"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tenant_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))
    owner_user_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    default_visibility: Mapped[str] = mapped_column(
        _enum(VISIBILITY_VALUES, "visibility"),
        nullable=False,
        default="private",
        server_default="private",
    )
    is_shareable: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    allow_item_override: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    share_token_hash: Mapped[str | None] = mapped_column(sa.Text)
    # Now comparable against asset.content_rating again.
    share_age_threshold: Mapped[str] = mapped_column(
        _enum(CONTENT_RATING_VALUES, "content_rating"),
        nullable=False,
        default="all_ages",
        server_default="all_ages",
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint("default_visibility <> 'catalog'", name="album_visibility_not_catalog"),
    )


class AlbumItem(Base):
    """v1's PK was ``(album_id, position)``: reordering an album was a
    primary-key update cascade.  Free to fix at drop-and-recreate.
    """

    __tablename__ = "album_item"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    album_id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False),
        sa.ForeignKey(f"{SCHEMA}.album.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), nullable=False
    )
    item_visibility: Mapped[str | None] = mapped_column(_enum(VISIBILITY_VALUES, "visibility"))

    __table_args__ = (
        sa.UniqueConstraint("album_id", "position", name="album_item_album_id_position_key"),
        sa.Index("album_item_album", "album_id", "position"),
    )


class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scope: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))
    owner_user_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))

    __table_args__ = (
        sa.CheckConstraint("scope IN ('global','tenant','user')", name="tag_scope_check"),
        sa.Index(
            "tag_name_scope",
            sa.text("lower(name)"),
            "scope",
            "tenant_id",
            "owner_user_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )


class AssetTag(Base):
    __tablename__ = "asset_tag"

    asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False),
        sa.ForeignKey(f"{SCHEMA}.tag.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False),
        primary_key=True,
        default="00000000-0000-0000-0000-000000000000",
        server_default="00000000-0000-0000-0000-000000000000",
    )
    owner_user_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))


class AlbumTag(Base):
    __tablename__ = "album_tag"

    album_id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False),
        sa.ForeignKey(f"{SCHEMA}.album.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False),
        sa.ForeignKey(f"{SCHEMA}.tag.id", ondelete="CASCADE"),
        primary_key=True,
    )


class ExternalRef(Base):
    __tablename__ = "external_ref"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    ref_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    ref_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    asset_sha256: Mapped[str] = mapped_column(
        Sha256Hex, sa.ForeignKey(f"{SCHEMA}.asset.sha256"), nullable=False
    )
    tenant_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))

    __table_args__ = (
        sa.UniqueConstraint("ref_type", "ref_id", name="external_ref_ref_type_ref_id_key"),
    )


class ServiceClient(Base):
    __tablename__ = "service_client"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    secret_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scopes: Mapped[str] = mapped_column(sa.Text, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[str] = mapped_column(
        sa.Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    actor_user_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))
    actor_service: Mapped[str | None] = mapped_column(sa.Text)
    tenant_id: Mapped[str | None] = mapped_column(sa.Uuid(as_uuid=False))
    asset_sha256: Mapped[str | None] = mapped_column(Sha256Hex)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default="{}"
    )
    ts: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, server_default=sa.func.now()
    )

    __table_args__ = (sa.Index("audit_event_asset", "asset_sha256", "ts"),)
