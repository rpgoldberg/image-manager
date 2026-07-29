"""Tests for search endpoints and tag management.

Tags key on ``asset.sha256``; tag names are matched case-insensitively via the
``lower(name)`` functional index rather than a CITEXT column.
"""

from app.models import Album, Tag


class TestCreateTag:
    def test_create_global_tag(self, client, auth_headers):
        r = client.post(
            "/tags", json={"name": "landscape", "scope": "global"}, headers=auth_headers
        )
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "landscape"
        assert "id" in data

    def test_create_tenant_tag(self, client, auth_headers):
        r = client.post(
            "/tags",
            json={
                "name": "internal",
                "scope": "tenant",
                "tenant_id": "11111111-2222-3333-4444-555555555555",
            },
            headers=auth_headers,
        )
        assert r.status_code == 200


class TestTagAsset:
    def test_tag_asset_by_id(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("tag-1")
        tag = Tag(name="sunset", scope="global")
        db_session.add(tag)
        db_session.commit()

        r = client.post(
            f"/tags/assets/{asset.sha256}", json={"tag_ids": [tag.id]}, headers=auth_headers
        )
        assert r.status_code == 200
        assert r.json()["count"] == 1

    def test_tag_asset_by_name(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("tag-2")
        tag = Tag(name="nature", scope="global")
        db_session.add(tag)
        db_session.commit()

        r = client.post(
            f"/tags/assets/{asset.sha256}",
            json={"tag_ids": [], "names": ["nature"]},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["count"] == 1

    def test_tag_name_match_is_case_insensitive(
        self, client, auth_headers, owned_asset, db_session
    ):
        """v1 leaned on a CITEXT column; the DDL uses a lower(name) index."""
        asset = owned_asset("tag-ci")
        db_session.add(Tag(name="Seaside", scope="global"))
        db_session.commit()

        r = client.post(
            f"/tags/assets/{asset.sha256}",
            json={"tag_ids": [], "names": ["SEASIDE"]},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["count"] == 1

    def test_tag_asset_deduplicates(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("tag-3")
        tag = Tag(name="dup", scope="global")
        db_session.add(tag)
        db_session.commit()

        client.post(
            f"/tags/assets/{asset.sha256}", json={"tag_ids": [tag.id]}, headers=auth_headers
        )
        r = client.post(
            f"/tags/assets/{asset.sha256}", json={"tag_ids": [tag.id]}, headers=auth_headers
        )
        assert r.status_code == 200


class TestTagAlbum:
    def test_tag_album(self, client, auth_headers, db_session):
        album = Album(title="Tagged Album")
        tag = Tag(name="travel", scope="global")
        db_session.add_all([album, tag])
        db_session.commit()

        r = client.post(
            f"/tags/albums/{album.id}", json={"tag_ids": [tag.id]}, headers=auth_headers
        )
        assert r.status_code == 200
        assert r.json()["count"] == 1


class TestSearchAssets:
    def test_search_by_mime(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("srch-1", mime="image/png")
        db_session.commit()

        r = client.get("/search/assets?query=png", headers=auth_headers)
        assert r.status_code == 200
        assert any(row["sha256"] == asset.sha256 for row in r.json()["results"])

    def test_search_no_results(self, client, auth_headers):
        r = client.get("/search/assets?query=nonexistent_xyz", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["results"] == []

    def test_search_by_tags(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("srch-2")
        tag = Tag(name="searchable", scope="global")
        db_session.add(tag)
        db_session.commit()
        client.post(
            f"/tags/assets/{asset.sha256}", json={"tag_ids": [tag.id]}, headers=auth_headers
        )

        r = client.get("/search/assets?tags=searchable", headers=auth_headers)
        assert r.status_code == 200
        assert any(row["sha256"] == asset.sha256 for row in r.json()["results"])

    def test_search_returns_all_when_no_filter(self, client, auth_headers, owned_asset, db_session):
        owned_asset("srch-3")
        db_session.commit()

        r = client.get("/search/assets", headers=auth_headers)
        assert r.status_code == 200
        assert len(r.json()["results"]) >= 1


class TestSearchAlbums:
    def test_search_by_title(self, client, auth_headers, db_session):
        album = Album(title="Unique Summer Vacation")
        db_session.add(album)
        db_session.commit()

        r = client.get("/search/albums?query=Summer", headers=auth_headers)
        assert r.status_code == 200
        assert any(row["id"] == album.id for row in r.json()["results"])

    def test_search_by_description(self, client, auth_headers, db_session):
        album = Album(title="X", description="Beach photos from Hawaii")
        db_session.add(album)
        db_session.commit()

        r = client.get("/search/albums?query=Hawaii", headers=auth_headers)
        assert r.status_code == 200
        assert any(row["id"] == album.id for row in r.json()["results"])

    def test_search_albums_by_tags(self, client, auth_headers, db_session):
        album = Album(title="Tagged")
        tag = Tag(name="album_tag", scope="global")
        db_session.add_all([album, tag])
        db_session.commit()

        client.post(f"/tags/albums/{album.id}", json={"tag_ids": [tag.id]}, headers=auth_headers)

        r = client.get("/search/albums?tags=album_tag", headers=auth_headers)
        assert r.status_code == 200
        assert any(row["id"] == album.id for row in r.json()["results"])
