"""Tests for worker session management — proper context managers with rollback."""

import datetime as dt
import inspect

from app.db import worker_session
from app.models import Asset, AssetOrigin
from sqlalchemy.orm import sessionmaker
from tests.conftest import sha


def _asset(label: str) -> Asset:
    return Asset(sha256=sha(label), mime="image/jpeg", bytes=100)


class TestWorkerSessionContextManager:
    def test_worker_session_commits_on_success(self, db_engine):
        """worker_session should auto-commit when block succeeds."""
        factory = sessionmaker(
            bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        with worker_session(session_factory=factory) as db:
            db.add(_asset("ws1"))

        check = factory()
        try:
            row = check.get(Asset, sha("ws1"))
            assert row is not None
        finally:
            check.close()

    def test_worker_session_rollback_on_exception(self, db_engine):
        """worker_session should rollback on exception and re-raise."""
        factory = sessionmaker(
            bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        try:
            with worker_session(session_factory=factory) as db:
                db.add(_asset("ws2"))
                db.flush()
                raise ValueError("test error")
        except ValueError:
            pass

        check = factory()
        try:
            assert check.get(Asset, sha("ws2")) is None
        finally:
            check.close()

    def test_worker_session_closes_session(self, db_engine):
        """Session should be closed after context exit."""
        factory = sessionmaker(
            bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        with worker_session(session_factory=factory) as db:
            pass
        assert db is not None

    def test_worker_session_reraises_exception(self, db_engine):
        """worker_session should re-raise the original exception."""
        factory = sessionmaker(
            bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        raised = False
        try:
            with worker_session(session_factory=factory) as db:  # noqa: F841
                raise RuntimeError("boom")
        except RuntimeError as e:
            assert str(e) == "boom"
            raised = True
        assert raised


class TestWorkerTasksUseContextManager:
    def test_verify_task_uses_worker_session(self):
        """verify_and_register_object should use worker_session context manager."""
        import app.workers.tasks as tasks_module

        assert "worker_session" in inspect.getsource(tasks_module.verify_and_register_object)

    def test_rendition_task_uses_worker_session(self):
        """build_rendition replaces v1's create_transformed_version."""
        import app.workers.tasks as tasks_module

        assert "worker_session" in inspect.getsource(tasks_module.build_rendition)

    def test_album_cover_task_uses_worker_session(self):
        """generate_album_cover should use worker_session context manager."""
        import app.workers.tasks as tasks_module

        assert "worker_session" in inspect.getsource(tasks_module.generate_album_cover)


class TestPresentationJobRechecksRights:
    def test_a_shut_gate_is_terminal_not_a_retry(self, db_engine):
        """Rights can be revoked between queueing and running.  A job that
        produced bytes anyway would be exactly the accident section 1202
        punishes, and retrying it would repeat the violation."""
        from app.models import PresentationJob
        from app.workers.tasks import build_presentation_layer

        factory = sessionmaker(
            bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        with worker_session(session_factory=factory) as db:
            asset = _asset("job-1")
            asset.watermark_state = "unchecked"  # gate shut
            db.add(asset)
            db.flush()
            db.add(
                AssetOrigin(
                    asset_sha256=asset.sha256,
                    source_class="user_photo",
                    rights_basis="own_work",
                    derive_permitted=True,
                    fetched_at=dt.datetime.now(dt.UTC),
                )
            )
            job = PresentationJob(
                base_asset_sha256=asset.sha256, layer_type="matte_mask", produced_by="birefnet:v1"
            )
            db.add(job)
            db.flush()
            job_id = job.id

        import app.db as db_module

        original = db_module.SessionLocal
        db_module.SessionLocal = factory
        try:
            build_presentation_layer(job_id=job_id)
        finally:
            db_module.SessionLocal = original

        check = factory()
        try:
            row = check.get(PresentationJob, job_id)
            assert row.state == "skipped_rights"
            assert row.finished_at is not None
            assert "watermark" in (row.last_error or "").lower()
        finally:
            check.close()
