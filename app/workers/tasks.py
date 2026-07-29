"""Worker tasks.

The v1 ``create_transformed_version`` task did one job for two different legal
situations: it baked new bytes for a resize *and* for a blur, whether or not
we owned the input.  That split in two here:

* :func:`build_rendition` — a technical re-encode of bytes we are already
  rehosting.  A cache.  No rights check beyond "this is not an expressive
  edit", because the marginal exposure of a smaller copy of a file we already
  serve is ~0.
* :func:`build_presentation_layer` — an expressive derivation.  It calls
  :func:`app.rights.assert_derivable` and records ``skipped_rights`` rather
  than producing bytes when the gate is shut.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import mimetypes
from typing import Any

from PIL import Image as PILImage
from sqlalchemy import select

from ..db import worker_session
from ..hashing import compute_phash, get_image_dimensions, sha256_bytes
from ..models import AlbumItem, Asset, AssetRendition, PresentationJob
from ..rights import RightsError, assert_derivable, assert_technical_transform, set_watermark_state
from ..s3 import get_s3, move_object
from . import celery_app

logger = logging.getLogger(__name__)


def _ext_for_mime(mime: str) -> str:
    if mime in ("image/jpeg", "image/jpg"):
        return "jpg"
    if mime == "image/png":
        return "png"
    if mime == "image/webp":
        return "webp"
    if mime == "image/tiff":
        return "tif"
    guess = mimetypes.guess_extension(mime) or ".bin"
    return guess.lstrip(".")


@celery_app.task(name="verify_and_register_object")
def verify_and_register_object(sha256: str, bucket: str, key: str, expected_sha256: str) -> None:
    """Verify the uploaded bytes and move them to their content address.

    ``asset.storage_key`` is a GENERATED column now — the content address IS
    the location — so this computes the same path rather than choosing one.
    """
    s3 = get_s3()
    obj = s3.get_object(Bucket=bucket, Key=key)
    data: bytes = obj["Body"].read()

    actual_sha = sha256_bytes(data)
    if actual_sha != expected_sha256:
        logger.error("sha_mismatch", extra={"sha256": sha256})
        return

    width, height = get_image_dimensions(data)
    p_hash = compute_phash(data)
    mime = obj.get("ContentType") or "image/jpeg"

    with worker_session() as db:
        asset = db.get(Asset, sha256)
        if not asset:
            return
        move_object(key, asset.storage_key)
        asset.width = width
        asset.height = height
        asset.bytes = len(data)
        asset.mime = mime
        asset.phash = p_hash


def enqueue_verify(*, sha256: str, bucket: str, key: str, expected_sha256: str) -> None:
    verify_and_register_object.delay(sha256, bucket, key, expected_sha256)


def _apply_transforms(data: bytes, spec: dict[str, Any]) -> tuple[bytes, str, int, int]:
    img = PILImage.open(io.BytesIO(data)).convert("RGB")
    resize = spec.get("resize")
    if resize:
        width = int(resize.get("width") or 0)
        height = int(resize.get("height") or 0)
        if width and height:
            img = img.resize((width, height))
        elif width:
            img = img.resize((width, int(img.height * (width / img.width))))
        elif height:
            img = img.resize((int(img.width * (height / img.height)), height))

    fmt = (spec.get("format") or "JPEG").upper()
    quality = int(spec.get("quality") or 85)
    out = io.BytesIO()
    if fmt == "WEBP":
        mime = "image/webp"
        img.save(out, format="WEBP", quality=quality)
    elif fmt == "PNG":
        mime = "image/png"
        img.save(out, format="PNG")
    else:
        mime = "image/jpeg"
        img.save(out, format="JPEG", quality=quality)
    return out.getvalue(), mime, img.width, img.height


@celery_app.task(name="build_rendition")
def build_rendition(
    *, sha256: str, transform_hash: str, transform: dict[str, Any], dest_key: str
) -> None:
    """Materialise a technical rendition.  A cache entry, not a derivative."""
    assert_technical_transform(transform)
    from ..config import get_settings

    settings = get_settings()
    s3 = get_s3()

    with worker_session() as db:
        asset = db.get(Asset, sha256)
        if not asset:
            return
        obj = s3.get_object(Bucket=settings.s3_bucket, Key=asset.storage_key)
        data: bytes = obj["Body"].read()
        data_out, mime_out, width, height = _apply_transforms(data, transform)
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=dest_key,
            Body=data_out,
            ContentType=mime_out,
            ACL="private",
        )
        rendition = db.get(AssetRendition, (sha256, transform_hash))
        if rendition is None:
            return
        rendition.mime = mime_out
        rendition.width = width
        rendition.height = height
        rendition.bytes = len(data_out)


def enqueue_rendition(
    *, sha256: str, transform_hash: str, transform: dict[str, Any], dest_key: str
) -> None:
    build_rendition.delay(
        sha256=sha256, transform_hash=transform_hash, transform=transform, dest_key=dest_key
    )


@celery_app.task(name="build_presentation_layer")
def build_presentation_layer(*, job_id: str) -> None:
    """Produce a mask/layer for a presentation.

    The gate is re-checked *here*, not only when the job was queued: rights
    can be revoked between queueing and running, and a job that produced bytes
    anyway would be exactly the accident section 1202 punishes.  A shut gate
    is a terminal ``skipped_rights``, not a retry.
    """
    with worker_session() as db:
        job = db.get(PresentationJob, job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        job.state = "running"
        job.attempts += 1
        try:
            assert_derivable(db, job.base_asset_sha256)
        except RightsError as exc:
            job.state = "skipped_rights"
            job.last_error = str(exc)
            job.finished_at = dt.datetime.now(dt.UTC)
            logger.warning("presentation_job_skipped_rights", extra={"job_id": job_id})
            return
        # Producing the mask bytes themselves is the matting pipeline's job;
        # this task exists to own the state machine around it.
        job.state = "succeeded"
        job.finished_at = dt.datetime.now(dt.UTC)


def enqueue_presentation_layer(*, job_id: str) -> None:
    build_presentation_layer.delay(job_id=job_id)


@celery_app.task(name="generate_album_cover")
def generate_album_cover(album_id: str) -> str:
    from ..config import get_settings

    settings = get_settings()
    s3 = get_s3()
    with worker_session() as db:
        items = (
            db.execute(
                select(AlbumItem)
                .where(AlbumItem.album_id == album_id)
                .order_by(AlbumItem.position)
                .limit(4)
            )
            .scalars()
            .all()
        )
        keys: list[str] = []
        for item in items:
            asset = db.get(Asset, item.asset_sha256)
            if asset:
                keys.append(asset.storage_key)

        tiles: list[PILImage.Image] = []
        for key in keys:
            obj = s3.get_object(Bucket=settings.s3_bucket, Key=key)
            data: bytes = obj["Body"].read()
            tile = PILImage.open(io.BytesIO(data)).convert("RGB")
            tile.thumbnail((256, 256))
            tiles.append(tile)
        if not tiles:
            canvas = PILImage.new("RGB", (512, 512), color=(240, 240, 240))
        else:
            canvas = PILImage.new("RGB", (512, 512))
            positions = [(0, 0), (256, 0), (0, 256), (256, 256)]
            for i, tile in enumerate(tiles[:4]):
                canvas.paste(tile, positions[i])
        out = io.BytesIO()
        canvas.save(out, format="WEBP", quality=80)
        data_out = out.getvalue()

        import hashlib

        digest = hashlib.sha256(data_out).hexdigest()[:8]
        dest_key = f"albums/{album_id}/cover-{digest}.webp"
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=dest_key,
            Body=data_out,
            ContentType="image/webp",
            ACL="public-read",
        )
        return dest_key


def enqueue_album_cover(album_id: str) -> str:
    # Returns the expected key; generation happens async.
    key = f"albums/{album_id}/cover-pending.webp"
    generate_album_cover.delay(album_id)
    return key


@celery_app.task(name="record_watermark_scan")
def record_watermark_scan(*, sha256: str, state: str, detector_version: str) -> None:
    """Detector output, applied monotonically.

    A re-run of an older detector must not flip 'watermarked' back to 'clean'
    and silently re-open the gate.
    """
    with worker_session() as db:
        asset = db.get(Asset, sha256)
        if asset is None:
            return
        try:
            set_watermark_state(db, asset, state, detector_version=detector_version)
        except RightsError as exc:
            logger.warning("watermark_downgrade_refused", extra={"sha256": sha256, "err": str(exc)})
