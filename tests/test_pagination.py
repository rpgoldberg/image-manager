"""Tests for cursor-based pagination on list endpoints.

v1 paged on the serial ``images.id``.  There is no serial any more, so asset
cursors are ``sha256`` and album cursors are ``id`` — both totally ordered, so
keyset pagination still holds.
"""

import datetime as dt
import uuid

from app.models import Album, Asset, AssetOrigin
from tests.conftest import sha


def _assets(db, prefix: str, count: int) -> list[str]:
    digests = []
    for i in range(count):
        digest = sha(f"{prefix}-{i}")
        db.add(Asset(sha256=digest, mime="image/jpeg", bytes=100))
        db.flush()
        db.add(
            AssetOrigin(
                asset_sha256=digest,
                source_class="user_photo",
                rights_basis="own_work",
                derive_permitted=True,
                fetched_at=dt.datetime.now(dt.UTC),
            )
        )
        digests.append(digest)
    db.commit()
    return sorted(digests)


class TestAssetSearchPagination:
    def test_default_limit(self, client, auth_headers, db_session):
        _assets(db_session, "pg", 5)

        r = client.get("/search/assets", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "results" in data
        assert "next_cursor" in data

    def test_limit_param(self, client, auth_headers, db_session):
        _assets(db_session, "lim", 10)

        r = client.get("/search/assets?limit=3", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert len(data["results"]) == 3
        assert data["next_cursor"] is not None

    def test_cursor_pagination(self, client, auth_headers, db_session):
        _assets(db_session, "cur", 5)

        r1 = client.get("/search/assets?limit=2", headers=auth_headers)
        d1 = r1.json()
        assert len(d1["results"]) == 2
        cursor = d1["next_cursor"]
        assert cursor is not None

        r2 = client.get(f"/search/assets?limit=2&after={cursor}", headers=auth_headers)
        d2 = r2.json()
        assert len(d2["results"]) == 2

        ids1 = {row["sha256"] for row in d1["results"]}
        ids2 = {row["sha256"] for row in d2["results"]}
        assert ids1.isdisjoint(ids2)

    def test_cursor_walk_covers_every_row_exactly_once(self, client, auth_headers, db_session):
        """Keyset pagination on a content address must not skip or repeat."""
        expected = set(_assets(db_session, "walk", 7))

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            url = "/search/assets?limit=2" + (f"&after={cursor}" if cursor else "")
            page = client.get(url, headers=auth_headers).json()
            seen.extend(row["sha256"] for row in page["results"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert sorted(seen) == sorted(expected)
        assert len(seen) == len(set(seen))

    def test_last_page_null_cursor(self, client, auth_headers, db_session):
        _assets(db_session, "last", 1)

        r = client.get("/search/assets?limit=100", headers=auth_headers)
        assert r.json()["next_cursor"] is None


class TestAlbumSearchPagination:
    def test_limit_param(self, client, auth_headers, db_session):
        for i in range(5):
            db_session.add(Album(id=str(uuid.UUID(int=i)), title=f"Album {i}"))
        db_session.commit()

        r = client.get("/search/albums?limit=2", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert len(data["results"]) == 2
        assert data["next_cursor"] is not None

    def test_cursor_pagination(self, client, auth_headers, db_session):
        for i in range(4):
            db_session.add(Album(id=str(uuid.UUID(int=i)), title=f"Page {i}"))
        db_session.commit()

        d1 = client.get("/search/albums?limit=2", headers=auth_headers).json()
        cursor = d1["next_cursor"]
        d2 = client.get(f"/search/albums?limit=2&after={cursor}", headers=auth_headers).json()

        ids1 = {row["id"] for row in d1["results"]}
        ids2 = {row["id"] for row in d2["results"]}
        assert ids1.isdisjoint(ids2)
