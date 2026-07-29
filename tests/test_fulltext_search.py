"""Tests for full-text search.

Validates the search helpers and route-level search.  SQLite tests exercise
the ILIKE fallback; tsvector is PostgreSQL-only.

BEHAVIOUR CHANGE: v1 searched ``images.storage_key``, which held an
operator-chosen path like ``photos/beach.png``, so "beach" was findable.
``asset.storage_key`` is a GENERATED content address now — there is no
filename anywhere in the table — so substring search over it can only match a
digest prefix.  Searching by human words needs ``tag`` or
``depiction.alt_text``, which is where those words actually live.
"""

from app.models import Album, Asset
from app.search import build_text_filter, is_postgres


class TestSearchHelper:
    def test_is_postgres_sqlite(self, db_session):
        """In test env (SQLite), is_postgres should return False."""
        assert is_postgres(db_session) is False

    def test_build_text_filter_returns_clause(self, db_session):
        """build_text_filter should return a usable SQLAlchemy clause."""
        clause = build_text_filter(db_session, "hello", Asset.mime, Asset.storage_key)
        assert clause is not None


class TestAssetFullTextSearch:
    def test_search_partial_match_on_content_address(
        self, client, auth_headers, owned_asset, db_session
    ):
        """ILIKE fallback should match substrings of the content address."""
        asset = owned_asset("ft1")
        db_session.commit()

        r = client.get(f"/search/assets?query={asset.sha256[:12]}", headers=auth_headers)
        assert r.status_code == 200
        assert asset.sha256 in [row["sha256"] for row in r.json()["results"]]

    def test_search_case_insensitive(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("ft2", mime="image/jpeg")
        db_session.commit()

        r = client.get("/search/assets?query=JPEG", headers=auth_headers)
        assert asset.sha256 in [row["sha256"] for row in r.json()["results"]]

    def test_search_by_mime_type(self, client, auth_headers, owned_asset, db_session):
        asset = owned_asset("ft3", mime="image/webp")
        db_session.commit()

        r = client.get("/search/assets?query=webp", headers=auth_headers)
        assert asset.sha256 in [row["sha256"] for row in r.json()["results"]]

    def test_empty_query_returns_all(self, client, auth_headers, owned_asset, db_session):
        owned_asset("ft4")
        db_session.commit()

        r = client.get("/search/assets", headers=auth_headers)
        assert r.status_code == 200
        assert len(r.json()["results"]) >= 1


class TestAlbumFullTextSearch:
    def test_search_title_case_insensitive(self, client, auth_headers, db_session):
        album = Album(title="Mountain Adventures")
        db_session.add(album)
        db_session.commit()

        r = client.get("/search/albums?query=mountain", headers=auth_headers)
        assert album.id in [row["id"] for row in r.json()["results"]]

    def test_search_description_partial(self, client, auth_headers, db_session):
        album = Album(title="X", description="Incredible sunset photography collection")
        db_session.add(album)
        db_session.commit()

        r = client.get("/search/albums?query=sunset", headers=auth_headers)
        assert album.id in [row["id"] for row in r.json()["results"]]

    def test_search_no_match(self, client, auth_headers):
        r = client.get("/search/albums?query=zzz_no_match_xyz", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["results"] == []
