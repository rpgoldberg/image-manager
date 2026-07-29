from __future__ import annotations

import hashlib
import io
from typing import BinaryIO

from imagehash import phash
from PIL import Image


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: BinaryIO, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def compute_phash(data: bytes) -> str:
    img = Image.open(io.BytesIO(data))
    hash_val = phash(img)
    return str(hash_val)


def get_image_dimensions(data: bytes) -> tuple[int, int]:
    img = Image.open(io.BytesIO(data))
    return img.width, img.height
