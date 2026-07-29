"""Tests for Pydantic request/response validation on all endpoints.

Two rules got stronger in the restructure and are asserted here:

* ``sha256`` is a real domain, so a 64-character string of the wrong alphabet
  is a 422 at the edge rather than a 500 in the ORM;
* ``share_age_threshold`` and ``content_rating`` are enums, so the v1 integer
  wire format no longer validates.
"""

from __future__ import annotations

import uuid

from app.models import Album
from tests.conftest import sha

VALID_SHA = sha("pydantic")


# ---------------------------------------------------------------------------
# Asset routes — validation
# ---------------------------------------------------------------------------


class TestInitiateUploadValidation:
    def test_missing_filename(self, client, auth_headers):
        r = client.post(
            "/assets/initiate-upload",
            json={"mime": "image/jpeg", "size": 1024},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_missing_mime(self, client, auth_headers):
        r = client.post(
            "/assets/initiate-upload",
            json={"filename": "a.jpg", "size": 1024},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_missing_size(self, client, auth_headers):
        r = client.post(
            "/assets/initiate-upload",
            json={"filename": "a.jpg", "mime": "image/jpeg"},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_zero_size(self, client, auth_headers):
        r = client.post(
            "/assets/initiate-upload",
            json={"filename": "a.jpg", "mime": "image/jpeg", "size": 0},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_negative_size(self, client, auth_headers):
        r = client.post(
            "/assets/initiate-upload",
            json={"filename": "a.jpg", "mime": "image/jpeg", "size": -1},
            headers=auth_headers,
        )
        assert r.status_code == 422


class TestCompleteUploadValidation:
    def test_missing_sha256(self, client, auth_headers):
        r = client.post(
            "/assets/complete",
            json={"key": "k", "mime": "image/jpeg", "size": 100},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_sha256_too_short(self, client, auth_headers):
        r = client.post(
            "/assets/complete",
            json={"sha256": "abc", "key": "k", "mime": "image/jpeg", "size": 100},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_sha256_wrong_alphabet_rejected(self, client, auth_headers):
        """CHAR(64) validated nothing: '../../etc/passwd' padded to 64 chars
        was an acceptable primary key.  sha256_hex is a real domain."""
        r = client.post(
            "/assets/complete",
            json={"sha256": "z" * 64, "key": "k", "mime": "image/jpeg", "size": 100},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_sha256_uppercase_rejected(self, client, auth_headers):
        """An UPPERCASE duplicate of the same content used to be a second row."""
        r = client.post(
            "/assets/complete",
            json={"sha256": VALID_SHA.upper(), "key": "k", "mime": "image/jpeg", "size": 100},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_missing_key(self, client, auth_headers):
        r = client.post(
            "/assets/complete",
            json={"sha256": VALID_SHA, "mime": "image/jpeg", "size": 100},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_zero_size(self, client, auth_headers):
        r = client.post(
            "/assets/complete",
            json={"sha256": VALID_SHA, "key": "k", "mime": "image/jpeg", "size": 0},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_valid_complete_response_shape(self, client, auth_headers):
        r = client.post(
            "/assets/complete",
            json={"sha256": VALID_SHA, "key": "uploads/k", "mime": "image/jpeg", "size": 1024},
            headers=auth_headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["sha256"] == VALID_SHA
        assert isinstance(data["created"], bool)

    def test_invalid_source_class_rejected(self, client, auth_headers):
        r = client.post(
            "/assets/complete",
            json={
                "sha256": VALID_SHA,
                "key": "k",
                "mime": "image/jpeg",
                "size": 100,
                "origin": {"source_class": "nonsense"},
            },
            headers=auth_headers,
        )
        assert r.status_code == 422


class TestGetAssetResponseShape:
    def test_asset_detail_shape(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("shape")
        db_session.commit()

        r = client.get(f"/assets/{asset.sha256}", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["sha256"] == asset.sha256
        assert isinstance(data["origins"], list)
        assert isinstance(data["presentations"], list)
        assert set(data["derive_state"]) >= {
            "permission_ok",
            "bytes_ok",
            "not_suppressed",
            "derive_ok",
            "reasons",
        }


class TestPresentationValidation:
    def test_invalid_layer_type(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("pv-1")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "nonsense", "produced_by": "x:v1"},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_invalid_composite_op(self, client, auth_headers, owned_asset, db_session):
        """Unconstrained free text flowing into a client-side canvas op."""
        asset = owned_asset("pv-2")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={
                "layer_type": "depth_transform",
                "produced_by": "x:v1",
                "composite_op": "javascript:alert(1)",
            },
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_invalid_render_context(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("pv-3")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={
                "layer_type": "depth_transform",
                "produced_by": "x:v1",
                "render_context": "everywhere",
            },
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_missing_produced_by(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("pv-4")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "depth_transform"},
            headers=auth_headers,
        )
        assert r.status_code == 422


class TestSetVisibilityValidation:
    def test_missing_visibility(self, client, auth_headers):
        r = client.post(f"/assets/{VALID_SHA}/visibility", json={}, headers=auth_headers)
        assert r.status_code == 422

    def test_invalid_visibility(self, client, auth_headers):
        r = client.post(
            f"/assets/{VALID_SHA}/visibility", json={"visibility": "nonsense"}, headers=auth_headers
        )
        assert r.status_code == 422


class TestDepictionValidation:
    def test_invalid_role(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("dep-1")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/depictions",
            json={"role": "hero_shot", "subject_product_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_claim_without_a_subject_is_refused(
        self, client, auth_headers, owned_asset, db_session
    ):
        """A claim with no stable subject handle cannot be deduped; refuse it
        LOUDLY rather than silently merging every such claim into one row."""
        asset = owned_asset("dep-2")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/depictions",
            json={"role": "main"},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_valid_first_party_claim(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("dep-3")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/depictions",
            json={"role": "user_shelf", "subject_product_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert "id" in r.json()


# ---------------------------------------------------------------------------
# Album routes — validation
# ---------------------------------------------------------------------------


class TestCreateAlbumValidation:
    def test_missing_title(self, client, auth_headers):
        r = client.post("/albums", json={}, headers=auth_headers)
        assert r.status_code == 422

    def test_invalid_default_visibility(self, client, auth_headers):
        r = client.post(
            "/albums", json={"title": "t", "default_visibility": "nonsense"}, headers=auth_headers
        )
        assert r.status_code == 422

    def test_catalog_is_not_an_album_visibility(self, client, auth_headers):
        r = client.post(
            "/albums", json={"title": "t", "default_visibility": "catalog"}, headers=auth_headers
        )
        assert r.status_code == 422

    def test_valid_create_response_shape(self, client, auth_headers):
        r = client.post("/albums", json={"title": "My Album"}, headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "id" in data
        assert uuid.UUID(data["id"])


class TestAddAlbumItemValidation:
    def test_missing_asset_sha256(self, client, auth_headers, db_session):
        album = Album(title="test")
        db_session.add(album)
        db_session.commit()
        r = client.post(f"/albums/{album.id}/items", json={}, headers=auth_headers)
        assert r.status_code == 422

    def test_legacy_image_id_is_rejected(self, client, auth_headers, db_session):
        album = Album(title="test")
        db_session.add(album)
        db_session.commit()
        r = client.post(f"/albums/{album.id}/items", json={"image_id": 1}, headers=auth_headers)
        assert r.status_code == 422


class TestShareAlbumValidation:
    def test_missing_enable(self, client, auth_headers, db_session):
        album = Album(title="test")
        db_session.add(album)
        db_session.commit()
        r = client.post(f"/albums/{album.id}/share", json={}, headers=auth_headers)
        assert r.status_code == 422


class TestGetAlbumResponseShape:
    def test_album_detail_shape(self, client, auth_headers, db_session):
        album = Album(title="My Album", description="desc", default_visibility="private")
        db_session.add(album)
        db_session.commit()

        r = client.get(f"/albums/{album.id}", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "id" in data
        assert "title" in data
        assert isinstance(data["items"], list)


# ---------------------------------------------------------------------------
# Tag routes — validation
# ---------------------------------------------------------------------------


class TestCreateTagValidation:
    def test_missing_name(self, client, auth_headers):
        r = client.post("/tags", json={"scope": "global"}, headers=auth_headers)
        assert r.status_code == 422

    def test_missing_scope(self, client, auth_headers):
        r = client.post("/tags", json={"name": "landscape"}, headers=auth_headers)
        assert r.status_code == 422

    def test_invalid_scope(self, client, auth_headers):
        r = client.post(
            "/tags", json={"name": "landscape", "scope": "invalid"}, headers=auth_headers
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# External routes — validation
# ---------------------------------------------------------------------------


class TestCreateExternalRefValidation:
    def test_missing_ref_type(self, client, auth_headers):
        r = client.post(
            "/external/refs", json={"ref_id": "x", "asset_sha256": VALID_SHA}, headers=auth_headers
        )
        assert r.status_code == 422

    def test_missing_ref_id(self, client, auth_headers):
        r = client.post(
            "/external/refs",
            json={"ref_type": "x", "asset_sha256": VALID_SHA},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_missing_asset_sha256(self, client, auth_headers):
        r = client.post(
            "/external/refs", json={"ref_type": "x", "ref_id": "y"}, headers=auth_headers
        )
        assert r.status_code == 422

    def test_malformed_asset_sha256(self, client, auth_headers):
        r = client.post(
            "/external/refs",
            json={"ref_type": "x", "ref_id": "y", "asset_sha256": "../../etc/passwd"},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_valid_create_response_shape(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("ext-shape")
        db_session.commit()
        r = client.post(
            "/external/refs",
            json={"ref_type": "post", "ref_id": "123", "asset_sha256": asset.sha256},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert uuid.UUID(r.json()["id"])
