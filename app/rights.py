"""The render gate — one definition, one text to audit.

The adjudicated DDL expresses this as a single view, ``asset_derive_state``.
A view cannot stop the application from compositing, so the same predicate
lives here, and every write and serve path calls it.  There is deliberately no
second copy: :func:`derive_state` is the only place the rule is written.

    New EXPRESSIVE bytes are created only when we own the input.

Everything below fails closed.  Section 1202 CMI-removal damages attach *per
violation* and are not mitigated by fair use, so an unknown fact is treated as
the fact that shuts the gate:

* ``watermark_state = 'unchecked'`` counts AS watermarked;
* ``cmi_present IS NULL`` counts AS present;
* a ``source_policy`` row older than :data:`POLICY_MAX_AGE` counts as denying;
* an asset with no origin at all counts as having no permission.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from .models import (
    Asset,
    AssetOrigin,
    AssetRendition,
    AssetSuppression,
    BlobPurgeQueue,
    Presentation,
    PresentationEvent,
    SourcePolicy,
    rating_rank,
)

#: A fail-closed control cannot contain a network call, so the per-source
#: policy is a local replica refreshed by a job.  STALE => CLOSED.
POLICY_MAX_AGE = dt.timedelta(days=7)

#: rights_basis values that constitute an AFFIRMATIVE third-party grant.
GRANTED_BASES = frozenset({"permission_granted", "own_work"})

#: rights_basis values a first-party (source_id IS NULL) origin may assert.
FIRST_PARTY_BASES = frozenset({"user_licence", "own_work"})

#: source_class values that mean "somebody else photographed this".
THIRD_PARTY_PHOTO_CLASSES = frozenset({"manufacturer_press", "retailer_studio"})

#: Transform keys whose presence turns a rendition cache into a
#: derivative-work factory.  Mirrors the DDL's ``rendition_is_technical``.
EXPRESSIVE_TRANSFORM_KEYS = frozenset(
    {"crop", "matte", "mask", "rotate", "skew", "remove_bg", "watermark"}
)

__all__ = [
    "EXPRESSIVE_TRANSFORM_KEYS",
    "POLICY_MAX_AGE",
    "DeriveState",
    "RightsError",
    "assert_derivable",
    "assert_technical_transform",
    "derive_ok",
    "derive_state",
    "disable_presentation",
    "latch_derive_ok",
    "rating_rank",
    "renderable_presentations",
    "set_watermark_state",
    "suppress_asset",
    "unlatch_guard",
]


class RightsError(Exception):
    """The gate is closed.  Routes translate this to HTTP 403."""


@dataclass(frozen=True)
class DeriveState:
    """The three independent halves of the gate, kept separate so a refusal
    can say *which* one closed and an operator can fix the right thing."""

    sha256: str
    permission_ok: bool
    bytes_ok: bool
    not_suppressed: bool
    reasons: tuple[str, ...] = field(default=())

    @property
    def derive_ok(self) -> bool:
        return self.permission_ok and self.bytes_ok and self.not_suppressed


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite hands back naive datetimes; PostgreSQL hands back aware ones."""
    if value is None:
        return None
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value


def _live_suppression(db: Session, sha256: str) -> AssetSuppression | None:
    return db.execute(
        select(AssetSuppression).where(
            AssetSuppression.asset_sha256 == sha256,
            AssetSuppression.lifted_at.is_(None),
        )
    ).scalar_one_or_none()


def derive_state(db: Session, sha256: str) -> DeriveState:
    """Evaluate the gate for one asset.

    This is the code twin of the ``asset_derive_state`` view.  If you change
    one, change the other; there is no third copy.
    """
    asset = db.get(Asset, sha256)
    if asset is None:
        return DeriveState(sha256, False, False, False, ("asset not found",))

    rows = db.execute(
        select(AssetOrigin, SourcePolicy)
        .outerjoin(SourcePolicy, SourcePolicy.source_id == AssetOrigin.source_id)
        .where(AssetOrigin.asset_sha256 == sha256)
    ).all()

    reasons: list[str] = []
    cutoff = dt.datetime.now(dt.UTC) - POLICY_MAX_AGE

    # --- PERMISSION, in precedence order -----------------------------------
    # (1) A DOCUMENTED third-party grant wins outright.  An absence-of-grant
    #     row (HLJ redistributing the same press shot) does NOT veto a real
    #     licence: blanket unanimity gets the many-origins case backwards,
    #     which is the case asset_origin exists to serve.
    granted = any(
        origin.derive_permitted
        and origin.rights_basis in GRANTED_BASES
        and policy is not None
        and policy.image_derive_ok
        and (_as_utc(policy.refreshed_at) or dt.datetime.min.replace(tzinfo=dt.UTC)) > cutoff
        for origin, policy in rows
    )

    # (2) A FIRST-PARTY claim wins ONLY if these bytes carry no press/retailer
    #     origin.  Content dedup makes "Ross's photo" and "the AmiAmi press
    #     shot" literally the same row; a user cannot self-certify bytes we
    #     downloaded from a retailer first.  This kills laundering with or
    #     without a watermark.
    first_party_claim = any(
        origin.derive_permitted
        and origin.rights_basis in FIRST_PARTY_BASES
        and origin.source_id is None
        for origin, _ in rows
    )
    carries_third_party_photo = any(
        origin.source_class in THIRD_PARTY_PHOTO_CLASSES for origin, _ in rows
    )
    permission_ok = granted or (first_party_claim and not carries_third_party_photo)

    if not permission_ok:
        if not rows:
            reasons.append("no origin recorded for these bytes")
        elif first_party_claim and carries_third_party_photo:
            reasons.append(
                "first-party claim rejected: these bytes also carry a "
                "manufacturer_press or retailer_studio origin"
            )
        else:
            reasons.append("no affirmative derive permission on any origin")

    # --- BYTE FACTS.  Unvotable, because they live on the bytes. -----------
    watermark_ok = asset.watermark_state == "clean"
    if not watermark_ok:
        reasons.append(
            f"watermark_state is {asset.watermark_state!r}; unchecked counts as watermarked"
        )

    # Section 1202 is about removing ANOTHER's copyright management
    # information.  A first-party photograph carrying the photographer's OWN
    # EXIF copyright must not be blocked by it -- that closed the gate on the
    # primary use case, silently.
    has_third_party_origin = any(origin.source_id is not None for origin, _ in rows)
    cmi_absent = asset.cmi_present is False  # COALESCE(cmi_present, true) = false
    cmi_ok = cmi_absent or not has_third_party_origin
    if not cmi_ok:
        reasons.append(
            "copyright management information present (or unchecked) on bytes "
            "acquired from a third party"
        )

    bytes_ok = watermark_ok and cmi_ok

    # --- TAKEDOWN VETO -----------------------------------------------------
    suppression = _live_suppression(db, sha256)
    not_suppressed = suppression is None
    if suppression is not None:
        reasons.append(f"asset is suppressed: {suppression.reason}")

    return DeriveState(sha256, permission_ok, bytes_ok, not_suppressed, tuple(reasons))


def derive_ok(db: Session, sha256: str) -> bool:
    return derive_state(db, sha256).derive_ok


def assert_derivable(db: Session, sha256: str) -> DeriveState:
    """Raise :class:`RightsError` unless the gate is open for *sha256*."""
    state = derive_state(db, sha256)
    if not state.derive_ok:
        raise RightsError(f"derivation refused for {sha256}: " + "; ".join(state.reasons))
    return state


def assert_technical_transform(transform: dict[str, Any]) -> None:
    """A rendition is a re-encode, never an expressive edit.

    Crop / matte / rotate belong to :class:`~app.models.Presentation`; if one
    appears in the rendition cache the cache has become a derivative-work
    factory.
    """
    offending = sorted(EXPRESSIVE_TRANSFORM_KEYS.intersection(transform))
    if offending:
        raise RightsError(
            "asset_rendition is a technical cache, not a derivative: "
            f"refusing expressive transform keys {offending}"
        )


# ---------------------------------------------------------------------------
# Monotonic byte facts.  A detector regression must not re-open a gate.
# ---------------------------------------------------------------------------


def set_watermark_state(
    db: Session, asset: Asset, state: str, *, detector_version: str | None = None
) -> None:
    """``watermark_state`` is MONOTONIC toward closed.

    Nothing in v2 stopped a re-run of an older detector flipping
    'watermarked' back to 'clean' and silently re-opening the gate.
    """
    if state not in ("clean", "watermarked", "unchecked"):
        raise RightsError(f"unknown watermark_state {state!r}")
    if asset.watermark_state == "watermarked" and state != "watermarked":
        raise RightsError(
            f"watermark_state is monotonic toward closed "
            f"({asset.watermark_state} -> {state}) on {asset.sha256}"
        )
    asset.watermark_state = state
    if detector_version is not None:
        asset.detector_version = detector_version


def latch_derive_ok(db: Session, asset: Asset) -> None:
    """Set the FK-enforceable "may be a derivation parent" latch.

    It must never be settable ahead of the gate, or an app bug re-opens
    derivation by UPDATE.
    """
    if asset.derive_ok_latched:
        return
    db.flush()
    if not derive_ok(db, asset.sha256):
        state = derive_state(db, asset.sha256)
        raise RightsError(
            f"derive_ok_latched refused: gate is closed for {asset.sha256}: "
            + "; ".join(state.reasons)
        )
    asset.derive_ok_latched = True


def unlatch_guard(session: Session) -> None:
    """Refuse any in-flight downgrade of a monotonic byte fact.

    Registered below as a ``before_flush`` hook so it applies to every session
    in the process, not only to callers who remember to ask.  Revocation goes
    through :func:`suppress_asset`, which is a veto and is auditable.
    """
    for obj in session.dirty:
        if not isinstance(obj, Asset):
            continue
        attrs = sa_inspect(obj).attrs
        latch_hist = attrs.derive_ok_latched.history
        if (
            latch_hist.has_changes()
            and latch_hist.deleted
            and latch_hist.deleted[0]
            and not obj.derive_ok_latched
        ):
            raise RightsError("derive_ok_latched is monotonic; revoke via asset_suppression")
        wm_hist = attrs.watermark_state.history
        if (
            wm_hist.has_changes()
            and wm_hist.deleted
            and wm_hist.deleted[0] == "watermarked"
            and obj.watermark_state != "watermarked"
        ):
            raise RightsError(
                f"watermark_state is monotonic toward closed "
                f"(watermarked -> {obj.watermark_state}) on {obj.sha256}"
            )


@event.listens_for(Session, "before_flush")
def _monotonic_before_flush(session: Session, _flush_context: Any, _instances: Any) -> None:
    unlatch_guard(session)


# ---------------------------------------------------------------------------
# Takedown
# ---------------------------------------------------------------------------


def suppress_asset(
    db: Session,
    sha256: str,
    *,
    reason: str,
    actor: str,
    notice_ref: str | None = None,
) -> AssetSuppression:
    """Record a takedown veto and purge the technical cache.

    Mirrors the ``asset_suppression_purges_renditions`` trigger: renditions
    are a cache of bytes we rehost, so suppression vetoes them too, and their
    storage keys go on the purge queue rather than being silently orphaned.
    """
    existing = db.get(AssetSuppression, sha256)
    if existing is not None and existing.lifted_at is None:
        return existing

    suppression = AssetSuppression(
        asset_sha256=sha256, reason=reason, suppressed_by=actor, notice_ref=notice_ref
    )
    db.merge(suppression) if existing is not None else db.add(suppression)

    renditions = (
        db.execute(select(AssetRendition).where(AssetRendition.base_asset_sha256 == sha256))
        .scalars()
        .all()
    )
    for rendition in renditions:
        if db.get(BlobPurgeQueue, rendition.storage_key) is None:
            db.add(
                BlobPurgeQueue(
                    storage_key=rendition.storage_key, reason=f"asset_suppression:{sha256}"
                )
            )
        db.delete(rendition)
    return suppression


# ---------------------------------------------------------------------------
# Presentations
# ---------------------------------------------------------------------------


def renderable_presentations(
    db: Session, base_sha256: str, render_context: str = "default"
) -> list[Presentation]:
    """What the serve path reads.

    A presentation composites only if it is enabled AND the gate is open for
    its base asset AND no suppressed ANCESTOR takes it down.  Callers must not
    query :class:`~app.models.Presentation` directly.
    """
    if not derive_ok(db, base_sha256):
        return []

    asset = db.get(Asset, base_sha256)
    if asset is None:
        return []
    # A suppressed ANCESTOR takes its descendants down.
    if asset.derived_from and _live_suppression(db, asset.derived_from) is not None:
        return []

    return list(
        db.execute(
            select(Presentation)
            .where(
                Presentation.base_asset_sha256 == base_sha256,
                Presentation.render_context == render_context,
                Presentation.enabled.is_(True),
            )
            .order_by(Presentation.z_index)
        )
        .scalars()
        .all()
    )


def create_presentation(
    db: Session,
    *,
    base_asset_sha256: str,
    layer_type: str,
    produced_by: str,
    actor: str,
    render_context: str = "default",
    layer_asset_sha256: str | None = None,
    z_index: int = 0,
    transform: dict[str, Any] | None = None,
    composite_op: str = "source-over",
) -> Presentation:
    """Create a render layer, gate-checked.

    A matte mask cannot exist over a photograph whose derivation was never
    permitted, so the check happens here rather than at serve time only.
    """
    assert_derivable(db, base_asset_sha256)
    presentation = Presentation(
        base_asset_sha256=base_asset_sha256,
        render_context=render_context,
        layer_type=layer_type,
        layer_asset_sha256=layer_asset_sha256,
        z_index=z_index,
        transform=transform or {},
        composite_op=composite_op,
        produced_by=produced_by,
    )
    db.add(presentation)
    db.flush()
    db.add(PresentationEvent(presentation_id=presentation.id, event="created", actor=actor))
    return presentation


def disable_presentation(
    db: Session, presentation: Presentation, *, reason: str, actor: str
) -> None:
    """The kill switch.  Dated disablement, never an erase."""
    if not reason or not reason.strip():
        raise RightsError("disabling a presentation requires a reason")
    presentation.enabled = False
    presentation.disabled_at = dt.datetime.now(dt.UTC)
    presentation.disabled_reason = reason
    db.add(
        PresentationEvent(
            presentation_id=presentation.id, event="disabled", reason=reason, actor=actor
        )
    )
