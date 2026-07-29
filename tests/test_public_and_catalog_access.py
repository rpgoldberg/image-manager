"""Tests for serve routes, public access, and visibility enforcement.

Serve keys on ``asset.sha256``.  Visibility comes from the grant
(``user_asset_link``) rather than from a version row, and a suppressed asset is
a 404 on every serve path — the takedown veto has to reach the bytes, not only
the layers drawn on top of them.
"""

import uuid

from app.models import UserAssetLink
from tests.conftest import sha

USER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TENANT = "11111111-2222-3333-4444-555555555555"


def _grant(db, asset, visibility="private", user_id=USER, tenant_id=TENANT):
    link = UserAssetLink(
        user_id=user_id, tenant_id=tenant_id, asset_sha256=asset.sha256, visibility=visibility
    )
    db.add(link)
    db.flush()
    return link


class TestServeAsset:
    def test_serve_private_with_auth(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("serve-1")
        _grant(db_session, asset, "private")
        db_session.commit()

        r = client.get(f"/serve/{asset.sha256}", headers=auth_headers, follow_redirects=False)
        assert r.status_code == 302
        assert "Location" in r.headers

    def test_serve_missing_404(self, client, auth_headers):
        r = client.get(f"/serve/{sha('absent')}", headers=auth_headers, follow_redirects=False)
        assert r.status_code == 404

    def test_serve_private_without_auth_403(self, client, owned_asset, db_session):
        asset = owned_asset("serve-anon")
        _grant(db_session, asset, "private")
        db_session.commit()
        r = client.get(f"/serve/{asset.sha256}", follow_redirects=False)
        assert r.status_code == 403

    def test_serve_suppressed_asset_404(self, client, auth_headers, owned_asset, db_session):
        from app.rights import suppress_asset

        asset = owned_asset("serve-suppressed")
        _grant(db_session, asset, "public")
        suppress_asset(db_session, asset.sha256, reason="DMCA 12", actor="legal")
        db_session.commit()

        r = client.get(f"/serve/{asset.sha256}", headers=auth_headers, follow_redirects=False)
        assert r.status_code == 404

    def test_serve_rendition(self, client, auth_headers, owned_asset, db_session):
        from app.models import AssetRendition

        asset = owned_asset("serve-rend")
        _grant(db_session, asset, "private")
        db_session.add(
            AssetRendition(
                base_asset_sha256=asset.sha256,
                transform_hash="w320",
                transform={"resize": {"width": 320}},
                mime="image/webp",
                storage_key="rendition/w320",
            )
        )
        db_session.commit()

        r = client.get(
            f"/serve/{asset.sha256}?rendition=w320", headers=auth_headers, follow_redirects=False
        )
        assert r.status_code == 302

    def test_serve_unknown_rendition_404(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("serve-norend")
        _grant(db_session, asset, "private")
        db_session.commit()
        r = client.get(
            f"/serve/{asset.sha256}?rendition=missing",
            headers=auth_headers,
            follow_redirects=False,
        )
        assert r.status_code == 404


class TestPublicServe:
    def test_public_serve_public_asset(self, client, owned_asset, db_session):
        asset = owned_asset("pub-1")
        _grant(db_session, asset, "public")
        db_session.commit()

        r = client.get(f"/public/{asset.sha256}", follow_redirects=False)
        assert r.status_code == 302
        assert "Location" in r.headers
        assert r.headers.get("Cache-Control") == "public, max-age=600"

    def test_public_serve_private_asset_403(self, client, owned_asset, db_session):
        asset = owned_asset("pub-2")
        _grant(db_session, asset, "private")
        db_session.commit()

        r = client.get(f"/public/{asset.sha256}", follow_redirects=False)
        assert r.status_code == 403

    def test_public_serve_suppressed_404(self, client, owned_asset, db_session):
        from app.rights import suppress_asset

        asset = owned_asset("pub-3")
        _grant(db_session, asset, "public")
        suppress_asset(db_session, asset.sha256, reason="DMCA 13", actor="legal")
        db_session.commit()

        r = client.get(f"/public/{asset.sha256}", follow_redirects=False)
        assert r.status_code == 404


class TestVisibilityEnforcement:
    def test_serve_sets_cache_headers(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("hdr-1")
        _grant(db_session, asset, "private")
        db_session.commit()

        r = client.get(f"/serve/{asset.sha256}", headers=auth_headers, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers.get("Cache-Control") == "private, max-age=600"
        assert r.headers.get("ETag") == asset.sha256

    def test_catalog_requires_service_scope(
        self, client, auth_headers, service_headers, owned_asset, db_session
    ):
        asset = owned_asset("cat-1")
        # Granted to somebody else, so the caller falls back to the most
        # permissive grant on the bytes rather than to their own.
        _grant(db_session, asset, "catalog", user_id=str(uuid.uuid4()))
        db_session.commit()

        assert (
            client.get(
                f"/serve/{asset.sha256}", headers=auth_headers, follow_redirects=False
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/serve/{asset.sha256}", headers=service_headers, follow_redirects=False
            ).status_code
            == 302
        )


class TestRenderManifest:
    def test_manifest_lists_enabled_layers(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("manifest-1")
        _grant(db_session, asset, "private")
        db_session.commit()
        client.post(
            f"/assets/{asset.sha256}/presentations",
            json={
                "layer_type": "depth_transform",
                "produced_by": "caseshelf:v1",
                "render_context": "case_shelf",
                "transform": {"rotateX": 8},
            },
            headers=auth_headers,
        )

        r = client.get(f"/render/{asset.sha256}?render_context=case_shelf", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert len(data["layers"]) == 1
        assert data["layers"][0]["transform"] == {"rotateX": 8}

    def test_manifest_is_empty_when_gate_shuts_after_the_fact(
        self, client, auth_headers, owned_asset, db_session
    ):
        """The layer was legal when it was made; a later takedown must stop it
        compositing without anyone remembering to delete it."""
        from app.rights import suppress_asset

        asset = owned_asset("manifest-2")
        _grant(db_session, asset, "private")
        db_session.commit()
        client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "x:v1", "transform": {}},
            headers=auth_headers,
        )
        assert (
            len(client.get(f"/render/{asset.sha256}", headers=auth_headers).json()["layers"]) == 1
        )

        suppress_asset(db_session, asset.sha256, reason="DMCA 14", actor="legal")
        db_session.commit()
        assert client.get(f"/render/{asset.sha256}", headers=auth_headers).status_code == 404
