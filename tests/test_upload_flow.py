"""Tests for the asset upload flow: initiate -> complete -> detail.

``images.id`` is gone; the content address is the key.  The upload path also
has a new obligation: an asset with no ``asset_origin`` row is audit-invisible,
so completing an upload records how we came to hold the bytes.
"""

from app.models import Asset, AssetOrigin, UserAssetLink
from tests.conftest import sha


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"


class TestInitiateUpload:
    def test_returns_presigned_fields(self, client, auth_headers):
        r = client.post(
            "/assets/initiate-upload",
            json={"filename": "photo.jpg", "mime": "image/jpeg", "size": 2048},
            headers=auth_headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert "url" in data
        assert "fields" in data
        assert "staging_key" in data
        assert "bucket" in data
        assert data["staging_key"].startswith("uploads/")

    def test_different_calls_get_different_staging_keys(self, client, auth_headers):
        payload = {"filename": "a.jpg", "mime": "image/jpeg", "size": 100}
        r1 = client.post("/assets/initiate-upload", json=payload, headers=auth_headers)
        r2 = client.post("/assets/initiate-upload", json=payload, headers=auth_headers)
        assert r1.json()["staging_key"] != r2.json()["staging_key"]


class TestCompleteUpload:
    def test_creates_asset_record(self, client, auth_headers, db_session):
        digest = sha("upload-1")
        r = client.post(
            "/assets/complete",
            json={"sha256": digest, "key": "uploads/k", "mime": "image/jpeg", "size": 1024},
            headers=auth_headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["created"] is True
        assert data["sha256"] == digest
        asset = db_session.get(Asset, digest)
        assert asset is not None

    def test_storage_key_is_the_content_address(self, client, auth_headers, db_session):
        """v2's free-form storage_key is what made the tombstone hack work on a
        table documented "never mutated"; it is a GENERATED column now."""
        digest = sha("upload-addr")
        client.post(
            "/assets/complete",
            json={"sha256": digest, "key": "uploads/k", "mime": "image/jpeg", "size": 1024},
            headers=auth_headers,
        )
        asset = db_session.get(Asset, digest)
        assert asset.storage_key == f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def test_idempotent_by_sha256(self, client, auth_headers):
        digest = sha("upload-2")
        payload = {"sha256": digest, "key": "uploads/k", "mime": "image/jpeg", "size": 512}
        r1 = client.post("/assets/complete", json=payload, headers=auth_headers)
        r2 = client.post("/assets/complete", json=payload, headers=auth_headers)
        assert r1.json()["sha256"] == r2.json()["sha256"]
        assert r1.json()["created"] is True
        assert r2.json()["created"] is False

    def test_creates_user_asset_link(self, client, auth_headers, db_session):
        digest = sha("upload-3")
        client.post(
            "/assets/complete",
            json={"sha256": digest, "key": "uploads/k", "mime": "image/jpeg", "size": 512},
            headers=auth_headers,
        )
        link = db_session.query(UserAssetLink).filter_by(asset_sha256=digest).first()
        assert link is not None
        assert link.role == "owner"

    def test_records_an_origin(self, client, auth_headers, db_session):
        """Bytes on disk with no rights record are audit-invisible."""
        digest = sha("upload-4")
        client.post(
            "/assets/complete",
            json={"sha256": digest, "key": "uploads/k", "mime": "image/jpeg", "size": 512},
            headers=auth_headers,
        )
        origin = db_session.query(AssetOrigin).filter_by(asset_sha256=digest).first()
        assert origin is not None
        assert origin.source_class == "user_photo"
        assert origin.rights_basis == "own_work"

    def test_scraped_origin_defaults_to_deny(self, client, auth_headers, db_session):
        """derive_permitted defaults FALSE, and 'unlicensed_norm' cannot carry
        an affirmative permission."""
        digest = sha("upload-scraped")
        r = client.post(
            "/assets/complete",
            json={
                "sha256": digest,
                "key": "uploads/k",
                "mime": "image/jpeg",
                "size": 512,
                "origin": {
                    "source_url": "https://retailer.test/item/1",
                    "source_class": "retailer_studio",
                    "rights_basis": "unlicensed_norm",
                },
            },
            headers=auth_headers,
        )
        assert r.status_code == 200
        origin = db_session.query(AssetOrigin).filter_by(asset_sha256=digest).first()
        assert origin.derive_permitted is False


class TestGetAsset:
    def test_returns_asset_with_gate_state(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("detail")
        db_session.commit()

        r = client.get(f"/assets/{asset.sha256}", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["sha256"] == asset.sha256
        assert data["derive_state"]["derive_ok"] is True
        assert len(data["origins"]) == 1
        assert isinstance(data["presentations"], list)

    def test_404_for_missing(self, client, auth_headers):
        r = client.get(f"/assets/{sha('never-uploaded')}", headers=auth_headers)
        assert r.status_code == 404
