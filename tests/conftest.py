from __future__ import annotations

import os

# Set test environment BEFORE any app imports so Settings picks up overrides
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["S3_ENDPOINT_URL"] = "http://localhost:9000"
os.environ["ENVIRONMENT"] = "test"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["ALLOW_DEV_TOKENS"] = "true"

import datetime as dt  # noqa: E402
import hashlib  # noqa: E402
import uuid  # noqa: E402
from typing import TYPE_CHECKING, Any  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

import pytest  # noqa: E402
from app.auth import create_token  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SCHEMA, Asset, AssetOrigin, Base, SourcePolicy  # noqa: E402
from app.sqlite_compat import install_sqlite_compat  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# ---------------------------------------------------------------------------
# Test digests
#
# sha256_hex is a real domain now: '\"a\" * 64' is still valid hex but 'ws1' * 22
# is not, and the ORM type rejects it.  Tests name their bytes instead of
# hand-rolling digests.
# ---------------------------------------------------------------------------


def sha(label: str) -> str:
    """A deterministic, *valid* sha256 digest for a named test fixture."""
    return hashlib.sha256(label.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Database fixtures -- in-memory SQLite, recreated per test
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_engine():  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite://",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # btrim() and friends: the CHECK constraints are written exactly as the
    # adjudicated PostgreSQL DDL writes them.
    install_sqlite_compat(engine)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):  # type: ignore[no-untyped-def]
        # The v3 schema lives in the `media` namespace.  SQLite gets one too.
        dbapi_conn.execute(f"ATTACH DATABASE ':memory:' AS {SCHEMA}")
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def db_session(db_engine) -> Generator[Session, None, None]:  # type: ignore[type-arg,no-untyped-def]
    TestSession = sessionmaker(
        bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    session = TestSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Settings override
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        redis_url="redis://localhost:6379/15",
        s3_endpoint_url="http://localhost:9000",
        environment="test",
        jwt_secret="test-secret",
        allow_dev_tokens=True,
    )


# ---------------------------------------------------------------------------
# FastAPI TestClient with dependency overrides
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_session: Session, test_settings: Settings) -> Generator[TestClient, None, None]:
    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    def _override_get_settings() -> Settings:
        return test_settings

    # Clear lru_cache so test settings are used
    get_settings.cache_clear()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_settings] = _override_get_settings

    # Patch Celery tasks to be no-ops so they don't require a broker
    with (
        patch("app.workers.tasks.verify_and_register_object.delay", new=MagicMock()),
        patch("app.workers.tasks.build_rendition.delay", new=MagicMock()),
        patch("app.workers.tasks.build_presentation_layer.delay", new=MagicMock()),
        patch("app.workers.tasks.generate_album_cover.delay", new=MagicMock()),
    ):
        yield TestClient(app, raise_server_exceptions=False)

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Media-schema factories
#
# Every asset needs at least one origin for the rights story to be auditable,
# and the render gate reads origins + source_policy, so building those by hand
# in each test would bury the assertion under setup.
# ---------------------------------------------------------------------------


@pytest.fixture()
def make_asset(db_session: Session):  # type: ignore[no-untyped-def]
    """Insert an `asset` row.  Byte facts default to the closed position."""

    def _make(
        label: str = "asset",
        *,
        mime: str = "image/jpeg",
        watermark_state: str = "unchecked",
        cmi_present: bool | None = None,
        content_rating: str = "unknown",
        derived_from: str | None = None,
        derived_under_basis: str | None = None,
        derive_ok_latched: bool = False,
        **kw: Any,
    ) -> Asset:
        asset = Asset(
            sha256=sha(label),
            mime=mime,
            watermark_state=watermark_state,
            cmi_present=cmi_present,
            content_rating=content_rating,
            derived_from=derived_from,
            derived_under_basis=derived_under_basis,
            derive_ok_latched=derive_ok_latched,
            **kw,
        )
        db_session.add(asset)
        db_session.flush()
        return asset

    return _make


@pytest.fixture()
def make_source(db_session: Session):  # type: ignore[no-untyped-def]
    """Insert a `source_policy` row.  Defaults deny everything, like the DDL."""

    def _make(
        site: str = "example.test",
        *,
        image_derive_ok: bool = False,
        image_rehost_ok: bool = False,
        image_hotlink_ok: bool = False,
        refreshed_at: dt.datetime | None = None,
    ) -> SourcePolicy:
        sp = SourcePolicy(
            source_id=str(uuid.uuid4()),
            site=site,
            image_derive_ok=image_derive_ok,
            image_rehost_ok=image_rehost_ok,
            image_hotlink_ok=image_hotlink_ok,
            refreshed_at=refreshed_at or dt.datetime.now(dt.UTC),
        )
        db_session.add(sp)
        db_session.flush()
        return sp

    return _make


@pytest.fixture()
def make_origin(db_session: Session):  # type: ignore[no-untyped-def]
    """Insert an `asset_origin` row."""

    def _make(
        asset: Asset,
        *,
        source: SourcePolicy | None = None,
        source_url: str | None = None,
        source_class: str = "unknown",
        rights_basis: str = "unknown",
        derive_permitted: bool = False,
        permission_ref: str | None = None,
        fetched_at: dt.datetime | None = None,
    ) -> AssetOrigin:
        origin = AssetOrigin(
            asset_sha256=asset.sha256,
            source_id=source.source_id if source else None,
            source_url=source_url,
            source_class=source_class,
            rights_basis=rights_basis,
            derive_permitted=derive_permitted,
            permission_ref=permission_ref,
            fetched_at=fetched_at or dt.datetime.now(dt.UTC),
        )
        db_session.add(origin)
        db_session.flush()
        return origin

    return _make


@pytest.fixture()
def owned_asset(db_session: Session, make_asset, make_origin):  # type: ignore[no-untyped-def]
    """An asset the gate should OPEN: first-party, own work, checked clean."""

    def _make(label: str = "owned", **kw: Any) -> Asset:
        asset = make_asset(label, watermark_state="clean", cmi_present=False, **kw)
        make_origin(
            asset,
            source_class="user_photo",
            rights_basis="own_work",
            derive_permitted=True,
        )
        db_session.flush()
        return asset

    return _make


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_headers(test_settings: Settings) -> dict[str, str]:
    """Return Authorization header for a regular user."""
    token = create_token(
        test_settings,
        subject="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        tenant_id="11111111-2222-3333-4444-555555555555",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def service_headers(test_settings: Settings) -> dict[str, str]:
    """Return Authorization header for a service client with assets:read scope."""
    token = create_token(
        test_settings,
        subject="service:test-svc",
        scopes=["assets:read"],
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def make_auth_headers(test_settings: Settings) -> Callable[..., dict[str, str]]:
    """Factory to create auth headers with custom claims."""

    def _make(
        *,
        subject: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        tenant_id: str | None = "11111111-2222-3333-4444-555555555555",
        scopes: list[str] | None = None,
    ) -> dict[str, str]:
        token = create_token(
            test_settings,
            subject=subject,
            tenant_id=tenant_id,
            scopes=scopes,
        )
        return {"Authorization": f"Bearer {token}"}

    return _make
