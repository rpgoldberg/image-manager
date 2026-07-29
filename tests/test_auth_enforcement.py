"""Tests that all protected routes return 401 when no auth token is provided.

Paths moved from ``/images/{id}`` to ``/assets/{sha256}`` in the restructure;
the auth obligation is unchanged, and the new routes carry it too.
"""

from __future__ import annotations

import uuid

from tests.conftest import sha

SHA = sha("auth-probe")
ALBUM = str(uuid.uuid4())
TAG = str(uuid.uuid4())

# ---------------------------------------------------------------------------
# Asset routes
# ---------------------------------------------------------------------------


class TestAssetRoutesRequireAuth:
    def test_initiate_upload_no_auth(self, client):
        r = client.post(
            "/assets/initiate-upload",
            json={"filename": "a.jpg", "mime": "image/jpeg", "size": 1024},
        )
        assert r.status_code == 401

    def test_initiate_upload_with_auth(self, client, auth_headers):
        r = client.post(
            "/assets/initiate-upload",
            json={"filename": "a.jpg", "mime": "image/jpeg", "size": 1024},
            headers=auth_headers,
        )
        assert r.status_code != 401

    def test_complete_upload_no_auth(self, client):
        r = client.post(
            "/assets/complete",
            json={"sha256": SHA, "key": "uploads/k", "mime": "image/jpeg", "size": 1024},
        )
        assert r.status_code == 401

    def test_complete_upload_with_auth(self, client, auth_headers):
        r = client.post(
            "/assets/complete",
            json={"sha256": SHA, "key": "uploads/k", "mime": "image/jpeg", "size": 1024},
            headers=auth_headers,
        )
        assert r.status_code != 401

    def test_get_asset_no_auth(self, client):
        r = client.get(f"/assets/{SHA}")
        assert r.status_code == 401

    def test_get_asset_with_auth(self, client, auth_headers):
        # 404 is fine — we just check it's not 401
        r = client.get(f"/assets/{SHA}", headers=auth_headers)
        assert r.status_code != 401

    def test_derive_state_no_auth(self, client):
        r = client.get(f"/assets/{SHA}/derive-state")
        assert r.status_code == 401

    def test_derive_state_with_auth(self, client, auth_headers):
        r = client.get(f"/assets/{SHA}/derive-state", headers=auth_headers)
        assert r.status_code != 401

    def test_create_rendition_no_auth(self, client):
        r = client.post(f"/assets/{SHA}/renditions", json={"transform": {}})
        assert r.status_code == 401

    def test_create_rendition_with_auth(self, client, auth_headers):
        r = client.post(f"/assets/{SHA}/renditions", json={"transform": {}}, headers=auth_headers)
        assert r.status_code != 401

    def test_create_presentation_no_auth(self, client):
        r = client.post(
            f"/assets/{SHA}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "x:v1"},
        )
        assert r.status_code == 401

    def test_create_presentation_with_auth(self, client, auth_headers):
        r = client.post(
            f"/assets/{SHA}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "x:v1"},
            headers=auth_headers,
        )
        assert r.status_code != 401

    def test_disable_presentation_no_auth(self, client):
        r = client.post(f"/assets/{SHA}/presentations/{uuid.uuid4()}/disable", json={"reason": "x"})
        assert r.status_code == 401

    def test_disable_presentation_with_auth(self, client, auth_headers):
        r = client.post(
            f"/assets/{SHA}/presentations/{uuid.uuid4()}/disable",
            json={"reason": "x"},
            headers=auth_headers,
        )
        assert r.status_code != 401

    def test_set_visibility_no_auth(self, client):
        r = client.post(f"/assets/{SHA}/visibility", json={"visibility": "public"})
        assert r.status_code == 401

    def test_set_visibility_with_auth(self, client, auth_headers):
        r = client.post(
            f"/assets/{SHA}/visibility", json={"visibility": "public"}, headers=auth_headers
        )
        assert r.status_code != 401

    def test_set_content_rating_no_auth(self, client):
        r = client.post(f"/assets/{SHA}/content-rating", json={"content_rating": "teen"})
        assert r.status_code == 401

    def test_set_content_rating_with_auth(self, client, auth_headers):
        r = client.post(
            f"/assets/{SHA}/content-rating", json={"content_rating": "teen"}, headers=auth_headers
        )
        assert r.status_code != 401

    def test_suppress_no_auth(self, client):
        r = client.post(f"/assets/{SHA}/suppress", json={"reason": "DMCA"})
        assert r.status_code == 401

    def test_suppress_with_auth(self, client, auth_headers):
        r = client.post(f"/assets/{SHA}/suppress", json={"reason": "DMCA"}, headers=auth_headers)
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# Depiction routes
# ---------------------------------------------------------------------------


class TestDepictionRoutesRequireAuth:
    def test_create_depiction_no_auth(self, client):
        r = client.post(f"/assets/{SHA}/depictions", json={"role": "main"})
        assert r.status_code == 401

    def test_create_depiction_with_auth(self, client, auth_headers):
        r = client.post(f"/assets/{SHA}/depictions", json={"role": "main"}, headers=auth_headers)
        assert r.status_code != 401

    def test_product_depictions_no_auth(self, client):
        r = client.get(f"/products/{uuid.uuid4()}/depictions")
        assert r.status_code == 401

    def test_product_depictions_with_auth(self, client, auth_headers):
        r = client.get(f"/products/{uuid.uuid4()}/depictions", headers=auth_headers)
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# Render manifest
# ---------------------------------------------------------------------------


class TestRenderRouteRequiresAuth:
    def test_render_no_auth(self, client):
        r = client.get(f"/render/{SHA}")
        assert r.status_code == 401

    def test_render_with_auth(self, client, auth_headers):
        r = client.get(f"/render/{SHA}", headers=auth_headers)
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# Album routes
# ---------------------------------------------------------------------------


class TestAlbumRoutesRequireAuth:
    def test_create_album_no_auth(self, client):
        r = client.post("/albums", json={"title": "test"})
        assert r.status_code == 401

    def test_create_album_with_auth(self, client, auth_headers):
        r = client.post("/albums", json={"title": "test"}, headers=auth_headers)
        assert r.status_code != 401

    def test_update_album_no_auth(self, client):
        r = client.put(f"/albums/{ALBUM}", json={"title": "updated"})
        assert r.status_code == 401

    def test_update_album_with_auth(self, client, auth_headers):
        r = client.put(f"/albums/{ALBUM}", json={"title": "updated"}, headers=auth_headers)
        assert r.status_code != 401

    def test_add_item_no_auth(self, client):
        r = client.post(f"/albums/{ALBUM}/items", json={"asset_sha256": SHA})
        assert r.status_code == 401

    def test_add_item_with_auth(self, client, auth_headers):
        r = client.post(f"/albums/{ALBUM}/items", json={"asset_sha256": SHA}, headers=auth_headers)
        assert r.status_code != 401

    def test_reorder_no_auth(self, client):
        r = client.put(f"/albums/{ALBUM}/items/reorder", json={"items": []})
        assert r.status_code == 401

    def test_reorder_with_auth(self, client, auth_headers):
        r = client.put(f"/albums/{ALBUM}/items/reorder", json={"items": []}, headers=auth_headers)
        assert r.status_code != 401

    def test_get_album_no_auth(self, client):
        r = client.get(f"/albums/{ALBUM}")
        assert r.status_code == 401

    def test_get_album_with_auth(self, client, auth_headers):
        r = client.get(f"/albums/{ALBUM}", headers=auth_headers)
        assert r.status_code != 401

    def test_share_album_no_auth(self, client):
        r = client.post(f"/albums/{ALBUM}/share", json={"enable": True})
        assert r.status_code == 401

    def test_share_album_with_auth(self, client, auth_headers):
        r = client.post(f"/albums/{ALBUM}/share", json={"enable": True}, headers=auth_headers)
        assert r.status_code != 401

    def test_album_cover_no_auth(self, client):
        r = client.get(f"/albums/cover/{ALBUM}")
        assert r.status_code == 401

    def test_album_cover_with_auth(self, client, auth_headers):
        r = client.get(f"/albums/cover/{ALBUM}", headers=auth_headers)
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# Tag routes
# ---------------------------------------------------------------------------


class TestTagRoutesRequireAuth:
    def test_create_tag_no_auth(self, client):
        r = client.post("/tags", json={"name": "landscape", "scope": "global"})
        assert r.status_code == 401

    def test_create_tag_with_auth(self, client, auth_headers):
        r = client.post(
            "/tags", json={"name": "landscape", "scope": "global"}, headers=auth_headers
        )
        assert r.status_code != 401

    def test_tag_asset_no_auth(self, client):
        r = client.post(f"/tags/assets/{SHA}", json={"tag_ids": [TAG]})
        assert r.status_code == 401

    def test_tag_asset_with_auth(self, client, auth_headers):
        r = client.post(f"/tags/assets/{SHA}", json={"tag_ids": []}, headers=auth_headers)
        assert r.status_code != 401

    def test_tag_album_no_auth(self, client):
        r = client.post(f"/tags/albums/{ALBUM}", json={"tag_ids": [TAG]})
        assert r.status_code == 401

    def test_tag_album_with_auth(self, client, auth_headers):
        r = client.post(f"/tags/albums/{ALBUM}", json={"tag_ids": []}, headers=auth_headers)
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# Search routes
# ---------------------------------------------------------------------------


class TestSearchRoutesRequireAuth:
    def test_search_assets_no_auth(self, client):
        r = client.get("/search/assets")
        assert r.status_code == 401

    def test_search_assets_with_auth(self, client, auth_headers):
        r = client.get("/search/assets", headers=auth_headers)
        assert r.status_code != 401

    def test_search_albums_no_auth(self, client):
        r = client.get("/search/albums")
        assert r.status_code == 401

    def test_search_albums_with_auth(self, client, auth_headers):
        r = client.get("/search/albums", headers=auth_headers)
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# External routes
# ---------------------------------------------------------------------------


class TestExternalRoutesRequireAuth:
    def test_create_external_ref_no_auth(self, client):
        r = client.post(
            "/external/refs", json={"ref_type": "foo", "ref_id": "bar", "asset_sha256": SHA}
        )
        assert r.status_code == 401

    def test_create_external_ref_with_auth(self, client, auth_headers):
        r = client.post(
            "/external/refs",
            json={"ref_type": "foo", "ref_id": "bar", "asset_sha256": SHA},
            headers=auth_headers,
        )
        assert r.status_code != 401

    def test_by_external_ref_no_auth(self, client):
        r = client.get("/external/assets/by-external-ref?ref_type=foo&ref_id=bar")
        assert r.status_code == 401

    def test_by_external_ref_with_auth(self, client, auth_headers):
        # 404 is fine
        r = client.get(
            "/external/assets/by-external-ref?ref_type=foo&ref_id=bar", headers=auth_headers
        )
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# Routes that SHOULD remain public
# ---------------------------------------------------------------------------


class TestPublicRoutes:
    def test_healthz_no_auth(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200

    def test_dev_token_no_auth(self, client):
        r = client.post("/auth/dev-token", json={"user_id": "test-user"})
        assert r.status_code == 200

    def test_public_serve_needs_no_token(self, client, owned_asset, db_session):
        from app.models import UserAssetLink

        asset = owned_asset("anon-public")
        db_session.add(
            UserAssetLink(user_id=str(uuid.uuid4()), asset_sha256=asset.sha256, visibility="public")
        )
        db_session.commit()
        r = client.get(f"/public/{asset.sha256}", follow_redirects=False)
        assert r.status_code == 302
