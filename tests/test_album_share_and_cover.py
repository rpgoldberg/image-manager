"""Tests for album CRUD, sharing, and cover generation.

``album.share_age_threshold`` is a ``content_rating`` now, not a SmallInteger,
so it is comparable against ``asset.content_rating`` again — v1 kept the
threshold but the restructure would have left it comparing against nothing.
"""

import uuid

from app.models import Album, AlbumItem


class TestAlbumCRUD:
    def test_create_album(self, client, auth_headers):
        r = client.post("/albums", json={"title": "Vacation"}, headers=auth_headers)
        assert r.status_code == 200
        assert "id" in r.json()

    def test_get_album(self, client, auth_headers, db_session):
        album = Album(title="Test", default_visibility="private")
        db_session.add(album)
        db_session.commit()

        r = client.get(f"/albums/{album.id}", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["title"] == "Test"
        assert data["items"] == []

    def test_update_album(self, client, auth_headers, db_session):
        album = Album(title="Old", default_visibility="private")
        db_session.add(album)
        db_session.commit()

        r = client.put(f"/albums/{album.id}", json={"title": "New"}, headers=auth_headers)
        assert r.status_code == 200
        db_session.refresh(album)
        assert album.title == "New"

    def test_get_missing_album_404(self, client, auth_headers):
        r = client.get(f"/albums/{uuid.uuid4()}", headers=auth_headers)
        assert r.status_code == 404


class TestAlbumItems:
    def test_add_item(self, client, auth_headers, owned_asset, db_session):
        album = Album(title="Album")
        db_session.add(album)
        asset = owned_asset("album-1")
        db_session.commit()

        r = client.post(
            f"/albums/{album.id}/items",
            json={"asset_sha256": asset.sha256},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["position"] == 0

    def test_add_multiple_items_auto_position(self, client, auth_headers, owned_asset, db_session):
        album = Album(title="Album")
        db_session.add(album)
        a1 = owned_asset("album-2")
        a2 = owned_asset("album-3")
        db_session.commit()

        r1 = client.post(
            f"/albums/{album.id}/items", json={"asset_sha256": a1.sha256}, headers=auth_headers
        )
        r2 = client.post(
            f"/albums/{album.id}/items", json={"asset_sha256": a2.sha256}, headers=auth_headers
        )
        assert r1.json()["position"] == 0
        assert r2.json()["position"] == 1

    def test_add_unknown_asset_404(self, client, auth_headers, db_session):
        from tests.conftest import sha

        album = Album(title="Album")
        db_session.add(album)
        db_session.commit()
        r = client.post(
            f"/albums/{album.id}/items",
            json={"asset_sha256": sha("not-ingested")},
            headers=auth_headers,
        )
        assert r.status_code == 404

    def test_reorder_items(self, client, auth_headers, owned_asset, db_session):
        album = Album(title="Album")
        db_session.add(album)
        asset = owned_asset("album-4")
        db_session.commit()

        client.post(
            f"/albums/{album.id}/items",
            json={"asset_sha256": asset.sha256, "position": 0},
            headers=auth_headers,
        )

        r = client.put(
            f"/albums/{album.id}/items/reorder",
            json={"items": [{"from_position": 0, "to_position": 5}]},
            headers=auth_headers,
        )
        assert r.status_code == 200
        db_session.expire_all()
        item = db_session.query(AlbumItem).filter_by(album_id=album.id).one()
        assert item.position == 5

    def test_reorder_does_not_move_the_primary_key(
        self, client, auth_headers, owned_asset, db_session
    ):
        """v1's PK was (album_id, position), so reordering was a primary-key
        update cascade.  album_item has its own surrogate id now."""
        album = Album(title="Album")
        db_session.add(album)
        asset = owned_asset("album-5")
        db_session.commit()
        client.post(
            f"/albums/{album.id}/items",
            json={"asset_sha256": asset.sha256, "position": 0},
            headers=auth_headers,
        )
        db_session.expire_all()
        before = db_session.query(AlbumItem).filter_by(album_id=album.id).one().id

        client.put(
            f"/albums/{album.id}/items/reorder",
            json={"items": [{"from_position": 0, "to_position": 3}]},
            headers=auth_headers,
        )
        db_session.expire_all()
        item = db_session.query(AlbumItem).filter_by(album_id=album.id).one()
        assert item.id == before
        assert item.position == 3


class TestAlbumSharing:
    def test_enable_sharing(self, client, auth_headers, db_session):
        album = Album(title="Share Me")
        db_session.add(album)
        db_session.commit()

        r = client.post(f"/albums/{album.id}/share", json={"enable": True}, headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["share_url"] is not None
        assert data["share_url"].startswith("/p/albums/")

    def test_disable_sharing(self, client, auth_headers, db_session):
        album = Album(title="Unshare")
        db_session.add(album)
        db_session.commit()

        client.post(f"/albums/{album.id}/share", json={"enable": True}, headers=auth_headers)
        r = client.post(f"/albums/{album.id}/share", json={"enable": False}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["share_url"] is None

    def test_share_with_age_threshold(self, client, auth_headers, db_session):
        album = Album(title="Rated")
        db_session.add(album)
        db_session.commit()

        r = client.post(
            f"/albums/{album.id}/share",
            json={"enable": True, "share_age_threshold": "adult"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        db_session.refresh(album)
        assert album.share_age_threshold == "adult"

    def test_share_threshold_rejects_a_bare_integer(self, client, auth_headers, db_session):
        """The v1 wire format (18) is no longer a content_rating."""
        album = Album(title="Legacy")
        db_session.add(album)
        db_session.commit()

        r = client.post(
            f"/albums/{album.id}/share",
            json={"enable": True, "share_age_threshold": 18},
            headers=auth_headers,
        )
        assert r.status_code == 422


class TestShareAgeGate:
    def test_unknown_rating_is_withheld_from_an_all_ages_share(self):
        """'unknown' must be the MOST restrictive, which the enum's own
        ordinal position is not."""
        from app.policy import can_view

        assert can_view(None, "public", None, "unknown", share_threshold="all_ages") is False
        assert can_view(None, "public", None, "all_ages", share_threshold="all_ages") is True
        assert can_view(None, "public", None, "teen", share_threshold="adult") is True
        assert can_view(None, "public", None, "adult", share_threshold="teen") is False


class TestAlbumCover:
    def test_cover_returns_key(self, client, auth_headers, db_session):
        album = Album(title="Cover Test")
        db_session.add(album)
        db_session.commit()

        r = client.get(f"/albums/cover/{album.id}", headers=auth_headers)
        assert r.status_code == 200
        assert "storage_key" in r.json()
