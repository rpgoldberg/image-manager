"""Tests for removal.

v1 soft-deleted an ``Image`` and an ``ImageVersion`` by stamping ``deleted_at``.
Neither column survives, and neither should:

* ``asset`` is "Never mutated.  Never deleted." — removal is
  ``asset_suppression``, a dated VETO that a later ingest cannot re-derive.
  v1's ``deleted_at`` was a flag any code path could clear.
* an ``ImageVersion`` delete is a ``presentation`` disable — the kill switch.
  Dated disablement, never an erase, with an append-only event trail, because
  "good faith demonstrable" IS a history.

``Album.deleted_at`` is unchanged: an album is our own record, not somebody
else's photograph.
"""

from app.models import Album, AssetSuppression, Presentation, PresentationEvent
from tests.conftest import sha


class TestSuppressAsset:
    def test_suppress_asset(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("sup-1")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/suppress",
            json={"reason": "DMCA notice 21", "notice_ref": "DMCA-21"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_suppressed_asset_not_served(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("sup-2")
        db_session.commit()
        client.post(
            f"/assets/{asset.sha256}/suppress", json={"reason": "DMCA 22"}, headers=auth_headers
        )
        assert client.get(f"/serve/{asset.sha256}", headers=auth_headers).status_code == 404

    def test_suppressed_asset_not_in_search(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("sup-3")
        db_session.commit()
        client.post(
            f"/assets/{asset.sha256}/suppress", json={"reason": "DMCA 23"}, headers=auth_headers
        )
        r = client.get("/search/assets", headers=auth_headers)
        assert asset.sha256 not in [row["sha256"] for row in r.json()["results"]]

    def test_suppress_missing_asset_404(self, client, auth_headers):
        r = client.post(
            f"/assets/{sha('phantom')}/suppress", json={"reason": "x"}, headers=auth_headers
        )
        assert r.status_code == 404

    def test_suppression_is_dated_and_attributed(
        self, client, auth_headers, owned_asset, db_session
    ):
        """The record still exists, with who and why — v1's deleted_at said
        neither."""
        asset = owned_asset("sup-4")
        db_session.commit()
        client.post(
            f"/assets/{asset.sha256}/suppress",
            json={"reason": "DMCA notice 24", "notice_ref": "DMCA-24"},
            headers=auth_headers,
        )
        db_session.expire_all()
        row = db_session.get(AssetSuppression, asset.sha256)
        assert row is not None
        assert row.suppressed_at is not None
        assert row.suppressed_by
        assert row.reason == "DMCA notice 24"
        assert row.notice_ref == "DMCA-24"

    def test_suppression_needs_a_reason(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("sup-5")
        db_session.commit()
        r = client.post(
            f"/assets/{asset.sha256}/suppress", json={"reason": ""}, headers=auth_headers
        )
        assert r.status_code == 422


class TestDeleteAlbum:
    def test_delete_album(self, client, auth_headers, db_session):
        album = Album(title="Delete Me")
        db_session.add(album)
        db_session.commit()

        r = client.delete(f"/albums/{album.id}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_deleted_album_not_in_get(self, client, auth_headers, db_session):
        album = Album(title="Gone")
        db_session.add(album)
        db_session.commit()

        client.delete(f"/albums/{album.id}", headers=auth_headers)
        r = client.get(f"/albums/{album.id}", headers=auth_headers)
        assert r.status_code == 404

    def test_deleted_album_not_in_search(self, client, auth_headers, db_session):
        album = Album(title="UniqueDeleteTest123")
        db_session.add(album)
        db_session.commit()

        client.delete(f"/albums/{album.id}", headers=auth_headers)
        r = client.get("/search/albums?query=UniqueDeleteTest123", headers=auth_headers)
        assert album.id not in [row["id"] for row in r.json()["results"]]

    def test_delete_missing_album_404(self, client, auth_headers):
        import uuid

        r = client.delete(f"/albums/{uuid.uuid4()}", headers=auth_headers)
        assert r.status_code == 404


class TestDisablePresentation:
    def test_disable_presentation(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("dis-1")
        db_session.commit()
        created = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "x:v1", "transform": {}},
            headers=auth_headers,
        ).json()

        r = client.post(
            f"/assets/{asset.sha256}/presentations/{created['id']}/disable",
            json={"reason": "DMCA 25"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_disabled_presentation_not_in_asset_detail(
        self, client, auth_headers, owned_asset, db_session
    ):
        asset = owned_asset("dis-2")
        db_session.commit()
        created = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "x:v1", "transform": {}},
            headers=auth_headers,
        ).json()
        client.post(
            f"/assets/{asset.sha256}/presentations/{created['id']}/disable",
            json={"reason": "DMCA 26"},
            headers=auth_headers,
        )

        r = client.get(f"/assets/{asset.sha256}", headers=auth_headers)
        assert r.status_code == 200
        assert created["id"] not in [p["id"] for p in r.json()["presentations"]]

    def test_disable_is_recorded_not_erased(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("dis-3")
        db_session.commit()
        created = client.post(
            f"/assets/{asset.sha256}/presentations",
            json={"layer_type": "depth_transform", "produced_by": "x:v1", "transform": {}},
            headers=auth_headers,
        ).json()
        client.post(
            f"/assets/{asset.sha256}/presentations/{created['id']}/disable",
            json={"reason": "DMCA 27"},
            headers=auth_headers,
        )

        db_session.expire_all()
        assert db_session.get(Presentation, created["id"]) is not None
        events = (
            db_session.query(PresentationEvent)
            .filter_by(presentation_id=created["id"])
            .order_by(PresentationEvent.at)
            .all()
        )
        assert [e.event for e in events] == ["created", "disabled"]
        assert events[1].reason == "DMCA 27"
