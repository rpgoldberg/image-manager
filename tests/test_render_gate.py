"""The render gate.

Section 1202 CMI-removal damages attach *per violation* and are not mitigated
by fair use, so the gate has to be structurally impossible to bypass rather
than a rule someone remembers.  The DDL expresses it as one view,
``asset_derive_state``.  :mod:`app.rights` expresses the same predicate in
code, because the view cannot stop the application from compositing anyway.

These tests are the code-side equivalent of the adjudication fixture set.
Every one of them asserts a CLOSED default: if a fact is unknown, the gate
stays shut.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.models import Asset, AssetSuppression, Presentation
from app.rights import (
    POLICY_MAX_AGE,
    RightsError,
    assert_derivable,
    assert_technical_transform,
    derive_state,
    latch_derive_ok,
    renderable_presentations,
    set_watermark_state,
    suppress_asset,
)
from tests.conftest import sha


def _ok(db, asset: Asset) -> bool:
    return derive_state(db, asset.sha256).derive_ok


# ---------------------------------------------------------------------------
# permission_ok
# ---------------------------------------------------------------------------


class TestPermission:
    def test_no_origin_at_all_is_closed(self, db_session, make_asset):
        """Bytes on disk with no rights record.  Audit-invisible, so: closed."""
        asset = make_asset("orphan", watermark_state="clean", cmi_present=False)
        state = derive_state(db_session, asset.sha256)
        assert state.permission_ok is False
        assert state.derive_ok is False

    def test_first_party_own_work_opens(self, db_session, make_asset, make_origin):
        asset = make_asset("mine", watermark_state="clean", cmi_present=False)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=True
        )
        assert _ok(db_session, asset) is True

    def test_derive_permitted_false_is_closed(self, db_session, make_asset, make_origin):
        """The gate's own flag.  Default DENY."""
        asset = make_asset("nope", watermark_state="clean", cmi_present=False)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=False
        )
        assert _ok(db_session, asset) is False

    def test_third_party_grant_with_fresh_permissive_policy_opens(
        self, db_session, make_asset, make_origin, make_source
    ):
        asset = make_asset("granted", watermark_state="clean", cmi_present=False)
        src = make_source("partner.test", image_derive_ok=True)
        make_origin(
            asset,
            source=src,
            source_class="manufacturer_press",
            rights_basis="permission_granted",
            permission_ref="MSA-2026-11",
            derive_permitted=True,
        )
        assert _ok(db_session, asset) is True

    def test_stale_source_policy_closes(self, db_session, make_asset, make_origin, make_source):
        """A fail-closed control cannot contain a network call.  STALE => CLOSED."""
        asset = make_asset("stale", watermark_state="clean", cmi_present=False)
        stale_at = dt.datetime.now(dt.UTC) - (POLICY_MAX_AGE + dt.timedelta(hours=1))
        src = make_source("partner.test", image_derive_ok=True, refreshed_at=stale_at)
        make_origin(
            asset,
            source=src,
            source_class="manufacturer_press",
            rights_basis="permission_granted",
            permission_ref="MSA-2026-11",
            derive_permitted=True,
        )
        assert _ok(db_session, asset) is False

    def test_source_policy_forbidding_derive_closes(
        self, db_session, make_asset, make_origin, make_source
    ):
        asset = make_asset("forbidden", watermark_state="clean", cmi_present=False)
        src = make_source("partner.test", image_derive_ok=False)
        make_origin(
            asset,
            source=src,
            source_class="manufacturer_press",
            rights_basis="permission_granted",
            permission_ref="MSA-2026-11",
            derive_permitted=True,
        )
        assert _ok(db_session, asset) is False

    def test_documented_grant_beats_an_absence_of_grant(
        self, db_session, make_asset, make_origin, make_source
    ):
        """The many-origins case section 3.2 exists to serve.

        The same press shot arrives from a partner who licensed it to us AND
        from a retailer who simply redistributes it.  Blanket unanimity gets
        this backwards: an absence-of-grant row does not veto a real licence.
        """
        asset = make_asset("sixretailers", watermark_state="clean", cmi_present=False)
        partner = make_source("partner.test", image_derive_ok=True)
        retailer = make_source("hlj.test", image_derive_ok=False)
        make_origin(
            asset,
            source=partner,
            source_class="manufacturer_press",
            rights_basis="permission_granted",
            permission_ref="MSA-2026-11",
            derive_permitted=True,
        )
        make_origin(
            asset,
            source=retailer,
            source_url="https://hlj.test/item/1",
            source_class="retailer_studio",
            rights_basis="unlicensed_norm",
            derive_permitted=False,
        )
        assert _ok(db_session, asset) is True

    def test_first_party_claim_cannot_launder_retailer_bytes(
        self, db_session, make_asset, make_origin, make_source
    ):
        """Content dedup makes "Ross's photo" and "the AmiAmi press shot"
        literally the same row.  A user cannot self-certify bytes we
        downloaded from a retailer first.
        """
        asset = make_asset("laundered", watermark_state="clean", cmi_present=False)
        retailer = make_source("amiami.test", image_derive_ok=False)
        make_origin(
            asset,
            source=retailer,
            source_url="https://amiami.test/item/1",
            source_class="manufacturer_press",
            rights_basis="unlicensed_norm",
            derive_permitted=False,
        )
        # ... and then someone uploads the identical bytes claiming a licence.
        make_origin(
            asset, source_class="user_photo", rights_basis="user_licence", derive_permitted=True
        )
        assert _ok(db_session, asset) is False

    def test_unlicensed_norm_cannot_carry_permission(
        self, db_session, make_asset, make_origin, make_source
    ):
        """The gate's premise is that permission is AFFIRMATIVE.

        v2 accepted ('unlicensed_norm', derive_permitted=TRUE).
        """
        from sqlalchemy.exc import IntegrityError

        asset = make_asset("norm", watermark_state="clean", cmi_present=False)
        src = make_source("mfc.test", image_derive_ok=True)
        with pytest.raises(IntegrityError):
            make_origin(
                asset,
                source=src,
                source_class="retailer_studio",
                rights_basis="unlicensed_norm",
                derive_permitted=True,
            )


# ---------------------------------------------------------------------------
# bytes_ok — unvotable, because these facts live on the bytes
# ---------------------------------------------------------------------------


class TestByteFacts:
    def test_unchecked_watermark_counts_as_watermarked(self, db_session, make_asset, make_origin):
        """Fail closed.  A watermark is visible pixels and we do not assume
        bytes are ours.
        """
        asset = make_asset("unchecked", watermark_state="unchecked", cmi_present=False)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=True
        )
        state = derive_state(db_session, asset.sha256)
        assert state.permission_ok is True, "permission side should be satisfied"
        assert state.bytes_ok is False
        assert state.derive_ok is False

    def test_watermarked_is_closed(self, db_session, make_asset, make_origin):
        asset = make_asset("marked", watermark_state="watermarked", cmi_present=False)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=True
        )
        assert _ok(db_session, asset) is False

    def test_cmi_present_with_third_party_origin_closes(
        self, db_session, make_asset, make_origin, make_source
    ):
        asset = make_asset("cmi3p", watermark_state="clean", cmi_present=True)
        src = make_source("partner.test", image_derive_ok=True)
        make_origin(
            asset,
            source=src,
            source_class="manufacturer_press",
            rights_basis="permission_granted",
            permission_ref="MSA-2026-11",
            derive_permitted=True,
        )
        state = derive_state(db_session, asset.sha256)
        assert state.permission_ok is True
        assert state.bytes_ok is False

    def test_own_exif_copyright_does_not_close_the_gate(self, db_session, make_asset, make_origin):
        """Section 1202 is about removing ANOTHER's copyright management
        information.  A first-party photograph carrying the photographer's OWN
        EXIF copyright must not be blocked by it -- that closed the gate on the
        primary use case, silently.
        """
        asset = make_asset("myexif", watermark_state="clean", cmi_present=True)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=True
        )
        assert _ok(db_session, asset) is True

    def test_unchecked_cmi_with_third_party_origin_closes(
        self, db_session, make_asset, make_origin, make_source
    ):
        """cmi_present IS NULL means nobody looked.  COALESCE(..., true)."""
        asset = make_asset("cmiunknown", watermark_state="clean", cmi_present=None)
        src = make_source("partner.test", image_derive_ok=True)
        make_origin(
            asset,
            source=src,
            source_class="manufacturer_press",
            rights_basis="permission_granted",
            permission_ref="MSA-2026-11",
            derive_permitted=True,
        )
        assert _ok(db_session, asset) is False

    def test_unchecked_cmi_first_party_only_opens(self, db_session, make_asset, make_origin):
        asset = make_asset("cmiunknown1p", watermark_state="clean", cmi_present=None)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=True
        )
        assert _ok(db_session, asset) is True


# ---------------------------------------------------------------------------
# suppression — the takedown VETO
# ---------------------------------------------------------------------------


class TestSuppression:
    def test_live_suppression_closes(self, db_session, owned_asset):
        asset = owned_asset("takedown")
        assert _ok(db_session, asset) is True
        suppress_asset(
            db_session, asset.sha256, reason="DMCA notice 41", actor="legal@figurecollecting"
        )
        db_session.flush()
        assert _ok(db_session, asset) is False

    def test_lifted_suppression_reopens(self, db_session, owned_asset):
        asset = owned_asset("lifted")
        suppress_asset(db_session, asset.sha256, reason="mistake", actor="legal")
        db_session.flush()
        row = db_session.get(AssetSuppression, asset.sha256)
        row.lifted_at = dt.datetime.now(dt.UTC)
        row.lifted_by = "legal"
        row.lift_reason = "counter-notice accepted"
        db_session.flush()
        assert _ok(db_session, asset) is True

    def test_a_later_ingest_cannot_reopen_a_closed_gate(
        self, db_session, owned_asset, make_origin, make_source
    ):
        """v2's worst failure: an unrelated later ingest RE-OPENED a closed
        gate, because revoking one source's origin could not close a gate
        another origin held open.  Suppression is a veto, not an origin.
        """
        asset = owned_asset("vetoed")
        suppress_asset(db_session, asset.sha256, reason="DMCA notice 42", actor="legal")
        db_session.flush()
        src = make_source("partner.test", image_derive_ok=True)
        make_origin(
            asset,
            source=src,
            source_url="https://partner.test/x",
            source_class="manufacturer_press",
            rights_basis="permission_granted",
            permission_ref="MSA-2026-11",
            derive_permitted=True,
        )
        db_session.flush()
        assert _ok(db_session, asset) is False

    def test_suppression_queues_renditions_for_purge(self, db_session, owned_asset):
        from app.models import AssetRendition, BlobPurgeQueue

        asset = owned_asset("cached")
        db_session.add(
            AssetRendition(
                base_asset_sha256=asset.sha256,
                transform_hash="w320",
                transform={"resize": {"width": 320}},
                mime="image/webp",
                storage_key="rend/w320",
            )
        )
        db_session.flush()

        suppress_asset(db_session, asset.sha256, reason="DMCA notice 43", actor="legal")
        db_session.flush()

        assert db_session.get(AssetRendition, (asset.sha256, "w320")) is None
        assert db_session.get(BlobPurgeQueue, "rend/w320") is not None


# ---------------------------------------------------------------------------
# assert_derivable — the chokepoint every write path calls
# ---------------------------------------------------------------------------


class TestAssertDerivable:
    def test_raises_with_a_reason_when_closed(self, db_session, make_asset, make_origin):
        asset = make_asset("closed", watermark_state="unchecked", cmi_present=False)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=True
        )
        with pytest.raises(RightsError) as exc:
            assert_derivable(db_session, asset.sha256)
        assert "watermark" in str(exc.value).lower()

    def test_passes_when_open(self, db_session, owned_asset):
        asset = owned_asset("open")
        assert_derivable(db_session, asset.sha256)

    def test_unknown_asset_is_closed(self, db_session):
        with pytest.raises(RightsError):
            assert_derivable(db_session, sha("never-ingested"))


# ---------------------------------------------------------------------------
# monotonicity — a detector regression must not re-open a section 1202 gate
# ---------------------------------------------------------------------------


class TestMonotonic:
    def test_watermarked_cannot_be_downgraded_to_clean(self, db_session, make_asset):
        asset = make_asset("regress", watermark_state="watermarked")
        with pytest.raises(RightsError):
            set_watermark_state(db_session, asset, "clean", detector_version="v0-old")

    def test_unchecked_can_be_resolved_either_way(self, db_session, make_asset):
        a = make_asset("resolve-clean", watermark_state="unchecked")
        set_watermark_state(db_session, a, "clean", detector_version="v2")
        assert a.watermark_state == "clean"

        b = make_asset("resolve-marked", watermark_state="unchecked")
        set_watermark_state(db_session, b, "watermarked", detector_version="v2")
        assert b.watermark_state == "watermarked"

    def test_latch_requires_an_open_gate(self, db_session, make_asset, make_origin):
        asset = make_asset("nolatch", watermark_state="unchecked", cmi_present=False)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=True
        )
        with pytest.raises(RightsError):
            latch_derive_ok(db_session, asset)
        assert asset.derive_ok_latched is False

    def test_latch_succeeds_when_gate_is_open(self, db_session, owned_asset):
        asset = owned_asset("latchable")
        latch_derive_ok(db_session, asset)
        assert asset.derive_ok_latched is True

    def test_latch_cannot_be_revoked_by_update(self, db_session, owned_asset):
        from app.rights import unlatch_guard

        asset = owned_asset("latched")
        latch_derive_ok(db_session, asset)
        db_session.flush()
        asset.derive_ok_latched = False
        with pytest.raises(RightsError):
            unlatch_guard(db_session)


# ---------------------------------------------------------------------------
# derivation — depth clamped to 1, parent must be permitted
# ---------------------------------------------------------------------------


class TestDerivation:
    def test_cannot_derive_from_an_unlatched_parent(self, db_session, make_asset):
        from sqlalchemy.exc import IntegrityError

        parent = make_asset("unlatched-parent", watermark_state="clean")
        db_session.flush()
        db_session.add(
            Asset(
                sha256=sha("child-of-unlatched"),
                mime="image/png",
                derived_from=parent.sha256,
                derived_under_basis="own_work",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_can_derive_from_a_latched_parent(self, db_session, owned_asset):
        parent = owned_asset("latched-parent")
        latch_derive_ok(db_session, parent)
        db_session.flush()
        child = Asset(
            sha256=sha("child-of-latched"),
            mime="image/png",
            derived_from=parent.sha256,
            derived_under_basis="own_work",
        )
        db_session.add(child)
        db_session.flush()
        assert child.derive_depth == 1
        assert child.is_derivation is True

    def test_derivation_depth_is_clamped_to_one(self, db_session, owned_asset):
        """A chain is the exact anti-pattern the retention layer bans."""
        from sqlalchemy.exc import IntegrityError

        parent = owned_asset("gp")
        latch_derive_ok(db_session, parent)
        db_session.flush()
        child = Asset(
            sha256=sha("mid"),
            mime="image/png",
            derived_from=parent.sha256,
            derived_under_basis="own_work",
            derive_ok_latched=True,
        )
        db_session.add(child)
        db_session.flush()

        grandchild = Asset(
            sha256=sha("grand"),
            mime="image/png",
            derived_from=child.sha256,
            derived_under_basis="own_work",
        )
        db_session.add(grandchild)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_self_derivation_is_impossible(self, db_session):
        from sqlalchemy.exc import IntegrityError

        digest = sha("narcissus")
        db_session.add(
            Asset(
                sha256=digest,
                mime="image/png",
                derived_from=digest,
                derived_under_basis="own_work",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()


# ---------------------------------------------------------------------------
# renderable_presentation — what the serve path reads
# ---------------------------------------------------------------------------


def _layer(base: Asset, mask: Asset | None = None, **kw) -> Presentation:
    return Presentation(
        base_asset_sha256=base.sha256,
        layer_type=kw.pop("layer_type", "depth_transform"),
        layer_asset_sha256=mask.sha256 if mask else None,
        produced_by=kw.pop("produced_by", "test:v1"),
        **kw,
    )


class TestRenderablePresentation:
    def test_open_gate_renders(self, db_session, owned_asset):
        asset = owned_asset("render-ok")
        db_session.add(_layer(asset, transform={"rotateX": 8}))
        db_session.flush()
        assert len(renderable_presentations(db_session, asset.sha256)) == 1

    def test_closed_gate_does_not_render(self, db_session, make_asset, make_origin):
        asset = make_asset("render-closed", watermark_state="unchecked", cmi_present=False)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=True
        )
        db_session.add(_layer(asset, transform={"rotateX": 8}))
        db_session.flush()
        assert renderable_presentations(db_session, asset.sha256) == []

    def test_disabled_layer_does_not_render(self, db_session, owned_asset):
        from app.rights import disable_presentation

        asset = owned_asset("render-killed")
        layer = _layer(asset, transform={"rotateX": 8})
        db_session.add(layer)
        db_session.flush()
        assert len(renderable_presentations(db_session, asset.sha256)) == 1

        disable_presentation(db_session, layer, reason="DMCA takedown", actor="legal")
        db_session.flush()
        assert renderable_presentations(db_session, asset.sha256) == []

    def test_a_disable_without_a_reason_is_refused(self, db_session, owned_asset):
        """v2's one-directional CHECK accepted enabled=TRUE with disabled_at
        and a reason still set: a failed takedown that looks like a completed
        one in every audit query."""
        from app.rights import disable_presentation

        asset = owned_asset("render-noreason")
        layer = _layer(asset, transform={"rotateX": 8})
        db_session.add(layer)
        db_session.flush()
        with pytest.raises(RightsError):
            disable_presentation(db_session, layer, reason="   ", actor="legal")

    def test_suppressed_ancestor_takes_descendants_down(self, db_session, owned_asset):
        parent = owned_asset("ancestor")
        latch_derive_ok(db_session, parent)
        db_session.flush()
        child = Asset(
            sha256=sha("descendant"),
            mime="image/png",
            watermark_state="clean",
            cmi_present=False,
            derived_from=parent.sha256,
            derived_under_basis="own_work",
        )
        db_session.add(child)
        db_session.flush()
        from app.models import AssetOrigin

        db_session.add(
            AssetOrigin(
                asset_sha256=child.sha256,
                source_class="derived_own",
                rights_basis="own_work",
                derive_permitted=True,
                fetched_at=dt.datetime.now(dt.UTC),
            )
        )
        db_session.add(_layer(child, transform={"rotateX": 8}))
        db_session.flush()
        assert len(renderable_presentations(db_session, child.sha256)) == 1

        suppress_asset(db_session, parent.sha256, reason="DMCA notice 44", actor="legal")
        db_session.flush()
        assert renderable_presentations(db_session, child.sha256) == []

    def test_render_context_is_isolated(self, db_session, owned_asset):
        asset = owned_asset("contexts")
        db_session.add(_layer(asset, render_context="case_shelf", transform={"rotateX": 8}))
        db_session.add(_layer(asset, render_context="detail", transform={"rotateX": 0}))
        db_session.flush()
        assert len(renderable_presentations(db_session, asset.sha256, "case_shelf")) == 1
        assert len(renderable_presentations(db_session, asset.sha256, "detail")) == 1
        assert renderable_presentations(db_session, asset.sha256, "default") == []


# ---------------------------------------------------------------------------
# asset_rendition is a CACHE, not a derivative-work factory
# ---------------------------------------------------------------------------


class TestTechnicalTransformOnly:
    @pytest.mark.parametrize(
        "spec",
        [
            {"crop": {"x": 0, "y": 0, "width": 10, "height": 10}},
            {"matte": "birefnet"},
            {"mask": "x"},
            {"rotate": 90},
            {"skew": 3},
            {"remove_bg": True},
            {"watermark": "logo"},
        ],
    )
    def test_expressive_edits_are_refused(self, spec):
        with pytest.raises(RightsError):
            assert_technical_transform(spec)

    @pytest.mark.parametrize(
        "spec",
        [
            {"resize": {"width": 320}},
            {"format": "webp", "quality": 80},
            {},
        ],
    )
    def test_technical_re_encodes_are_allowed(self, spec):
        assert_technical_transform(spec)
