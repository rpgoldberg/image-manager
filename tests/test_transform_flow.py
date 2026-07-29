"""Tests for what replaced ``ImageVersion``.

Its two real jobs split by the question "do we own the input":

* a technical re-encode of bytes we are already rehosting is an
  :class:`~app.models.AssetRendition` — a cache;
* an expressive edit is a :class:`~app.models.Presentation` — render layers,
  no new bytes.

The old ``expose-safe-alt`` endpoint (which baked a *blurred copy* of an image
whether or not we owned it) is gone: blurring is expressive, so it is a
presentation layer now and goes through the gate.
"""

from app.models import AssetRendition, ExternalRef, Presentation
from tests.conftest import sha


class TestRenditions:
    def test_create_rendition(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("rend-1")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/renditions",
            json={"transform": {"resize": {"width": 320}, "format": "webp"}},
            headers=auth_headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["sha256"] == asset.sha256
        assert data["storage_key"].startswith("rendition/")
        assert db_session.get(AssetRendition, (asset.sha256, data["transform_hash"])) is not None

    def test_rendition_is_idempotent_per_transform(
        self, client, auth_headers, owned_asset, db_session
    ):
        asset = owned_asset("rend-2")
        db_session.commit()
        body = {"transform": {"resize": {"width": 320}}}
        r1 = client.post(f"/assets/{asset.sha256}/renditions", json=body, headers=auth_headers)
        r2 = client.post(f"/assets/{asset.sha256}/renditions", json=body, headers=auth_headers)
        assert r1.json()["transform_hash"] == r2.json()["transform_hash"]
        assert r1.json()["storage_key"] == r2.json()["storage_key"]

    def test_expressive_transform_is_refused(self, client, auth_headers, owned_asset, db_session):
        """If a crop appears here the cache has become a derivative-work
        factory."""
        asset = owned_asset("rend-3")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/renditions",
            json={"transform": {"crop": {"x": 0, "y": 0, "width": 10, "height": 10}}},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_rendition_on_missing_asset_404(self, client, auth_headers):
        r = client.post(
            f"/assets/{sha('nope')}/renditions",
            json={"transform": {"resize": {"width": 10}}},
            headers=auth_headers,
        )
        assert r.status_code == 404


class TestPresentations:
    def test_create_presentation_when_gate_open(
        self, client, auth_headers, owned_asset, db_session
    ):
        asset = owned_asset("pres-1")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={
                "layer_type": "depth_transform",
                "produced_by": "caseshelf:v1",
                "render_context": "case_shelf",
                "transform": {"rotateX": 8},
            },
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert db_session.get(Presentation, r.json()["id"]) is not None

    def test_presentation_refused_when_gate_closed(
        self, client, auth_headers, make_asset, make_origin, db_session
    ):
        """A matte mask cannot exist over a photograph whose derivation was
        never permitted."""
        asset = make_asset("pres-closed", watermark_state="unchecked", cmi_present=False)
        make_origin(
            asset, source_class="user_photo", rights_basis="own_work", derive_permitted=True
        )
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "caseshelf:v1", "transform": {}},
            headers=auth_headers,
        )
        assert r.status_code == 403
        assert "watermark" in r.json()["detail"].lower()

    def test_disable_presentation_is_the_kill_switch(
        self, client, auth_headers, owned_asset, db_session
    ):
        asset = owned_asset("pres-kill")
        db_session.commit()
        created = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "caseshelf:v1", "transform": {}},
            headers=auth_headers,
        ).json()

        r = client.post(
            f"/assets/{asset.sha256}/presentations/{created['id']}/disable",
            json={"reason": "DMCA takedown 41"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        db_session.expire_all()
        layer = db_session.get(Presentation, created["id"])
        assert layer.enabled is False
        assert layer.disabled_at is not None
        assert layer.disabled_reason == "DMCA takedown 41"

    def test_disable_requires_a_reason(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("pres-noreason")
        db_session.commit()
        created = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "caseshelf:v1", "transform": {}},
            headers=auth_headers,
        ).json()
        r = client.post(
            f"/assets/{asset.sha256}/presentations/{created['id']}/disable",
            json={"reason": ""},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_disable_wrong_asset_404(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("pres-wrong")
        other = owned_asset("pres-other")
        db_session.commit()
        created = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "x:v1", "transform": {}},
            headers=auth_headers,
        ).json()
        r = client.post(
            f"/assets/{other.sha256}/presentations/{created['id']}/disable",
            json={"reason": "wrong asset"},
            headers=auth_headers,
        )
        assert r.status_code == 404


class TestVisibility:
    def test_set_visibility_on_the_grant(self, client, auth_headers, db_session):
        """Visibility is a property of the grant, not of a rendering."""
        digest = sha("vis-1")
        client.post(
            "/assets/complete",
            json={"sha256": digest, "key": "uploads/k", "mime": "image/jpeg", "size": 10},
            headers=auth_headers,
        )
        r = client.post(
            f"/assets/{digest}/visibility", json={"visibility": "public"}, headers=auth_headers
        )
        assert r.status_code == 200

        from app.models import UserAssetLink

        db_session.expire_all()
        link = db_session.query(UserAssetLink).filter_by(asset_sha256=digest).first()
        assert link.visibility == "public"

    def test_set_visibility_without_a_grant_404(
        self, client, auth_headers, owned_asset, db_session
    ):
        asset = owned_asset("vis-nogrant")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/visibility",
            json={"visibility": "public"},
            headers=auth_headers,
        )
        assert r.status_code == 404


class TestExternalRefs:
    def test_create_and_lookup(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("ext-1")
        db_session.commit()
        r = client.post(
            "/external/refs",
            json={"ref_type": "post", "ref_id": "post-42", "asset_sha256": asset.sha256},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert db_session.get(ExternalRef, r.json()["id"]) is not None

        r = client.get(
            "/external/assets/by-external-ref?ref_type=post&ref_id=post-42", headers=auth_headers
        )
        assert r.status_code == 200
        data = r.json()
        assert data["sha256"] == asset.sha256
        assert "url" in data

    def test_lookup_missing_ref_404(self, client, auth_headers):
        r = client.get(
            "/external/assets/by-external-ref?ref_type=nope&ref_id=nope", headers=auth_headers
        )
        assert r.status_code == 404

    def test_ref_to_unknown_asset_404(self, client, auth_headers):
        r = client.post(
            "/external/refs",
            json={"ref_type": "post", "ref_id": "ghost", "asset_sha256": sha("ghost")},
            headers=auth_headers,
        )
        assert r.status_code == 404

    def test_suppressed_asset_is_not_reachable_by_ref(
        self, client, auth_headers, owned_asset, db_session
    ):
        from app.rights import suppress_asset

        asset = owned_asset("ext-suppressed")
        db_session.commit()
        client.post(
            "/external/refs",
            json={"ref_type": "post", "ref_id": "post-99", "asset_sha256": asset.sha256},
            headers=auth_headers,
        )
        suppress_asset(db_session, asset.sha256, reason="DMCA 7", actor="legal")
        db_session.commit()

        r = client.get(
            "/external/assets/by-external-ref?ref_type=post&ref_id=post-99", headers=auth_headers
        )
        assert r.status_code == 404
