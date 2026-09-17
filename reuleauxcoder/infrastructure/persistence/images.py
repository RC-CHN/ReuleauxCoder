"""Session image variants and a bounded, independently evictable original cache."""

from __future__ import annotations

import base64
from dataclasses import asdict, replace
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from PIL import Image, ImageOps, UnidentifiedImageError

from reuleauxcoder.domain.images import ImageConfig, ImageReference


class ImageDeliveryError(ValueError):
    """An image cannot be delivered within its configured limits."""


_SESSION = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_QUALITIES = (80, 60, 40, 20)
_EDGES = (2000, 1000, 768, 512, 384, 256)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ImageStore:
    def __init__(self, sessions_dir: Path | str, config: ImageConfig | None = None):
        self.sessions_dir = Path(sessions_dir).resolve()
        self.config = config or ImageConfig()
        self._lock = threading.RLock()

    def _root(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
            raise ValueError("Invalid image session")
        root = self.sessions_dir / session_id / "images"
        if not root.resolve().is_relative_to(self.sessions_dir):
            raise ValueError("Image storage escapes session directory")
        return root

    def _key(
        self,
        attachment_id: str,
        region: tuple | None,
        full_resolution: bool,
        detail: bool,
    ) -> str:
        return sha256(
            json.dumps(
                {
                    "version": 1,
                    "original": attachment_id,
                    "region": region,
                    "full_resolution": full_resolution,
                    "detail": detail,
                    "config": asdict(self.config),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def import_bytes(
        self, session_id: str, data: bytes, *, name: str = "image"
    ) -> ImageReference:
        if (
            not isinstance(data, bytes)
            or not data
            or len(data) > self.config.import_max_bytes
        ):
            raise ImageDeliveryError("Image exceeds the import byte limit or is empty")
        attachment_id = sha256(data).hexdigest()
        root = self._root(session_id)
        with self._lock:
            image = self._variant(root, attachment_id, data, name=name)
            originals = root / "originals"
            if len(data) <= self.config.originals_cache_max_bytes:
                path = originals / attachment_id
                if not path.exists():
                    _atomic_write(path, data)
            self._sweep_originals(originals)
            return image

    def read_image(
        self,
        session_id: str,
        attachment_id: str,
        *,
        region: tuple[int, int, int, int] | None = None,
        full_resolution: bool = False,
    ) -> ImageReference:
        if not _HASH.fullmatch(attachment_id):
            raise ValueError("Invalid attachment ID")
        if region is not None and (
            len(region) != 4
            or any(type(value) is not int for value in region)
            or min(region[:2]) < 0
            or min(region[2:]) < 1
        ):
            raise ValueError("region must be [x, y, width, height] in original pixels")
        root = self._root(session_id)
        with self._lock:
            key = self._key(attachment_id, region, full_resolution, True)
            cached = root / "metadata" / f"{key}.json"
            if cached.is_file():
                return ImageReference(**json.loads(cached.read_text()))
            original = root / "originals" / attachment_id
            if not original.is_file():
                raise ImageDeliveryError(
                    "Original image cache was evicted; re-import the original for detail"
                )
            data = original.read_bytes()
            if sha256(data).hexdigest() != attachment_id:
                raise ImageDeliveryError("Original image checksum mismatch")
            return self._variant(
                root,
                attachment_id,
                data,
                region=region,
                full_resolution=full_resolution,
                detail=True,
            )

    def data_url(self, session_id: str, image: ImageReference) -> str:
        path = self._root(session_id) / "variants" / image.variant_id
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise ImageDeliveryError("Saved image version is unavailable") from error
        if (
            len(data) != image.size_bytes
            or sha256(data).hexdigest() != image.variant_id
        ):
            raise ImageDeliveryError("Saved image version checksum mismatch")
        return f"data:{image.mime_type};base64,{base64.b64encode(data).decode('ascii')}"

    def validate_reference(self, session_id: str, image: ImageReference) -> None:
        """Admission only accepts an imported, ordinary input version in this session."""
        root = self._root(session_id)
        metadata = (
            root
            / "metadata"
            / f"{self._key(image.attachment_id, None, False, False)}.json"
        )
        try:
            saved = ImageReference(**json.loads(metadata.read_text()))
        except FileNotFoundError as error:
            raise ImageDeliveryError(
                "Image was not imported into this session; attach it again"
            ) from error
        if replace(image, turn_id=None) != saved:
            raise ImageDeliveryError(
                "Image reference does not match its saved metadata"
            )
        self.data_url(session_id, image)

    def _variant(
        self,
        root: Path,
        attachment_id: str,
        data: bytes,
        *,
        name: str = "image",
        region: tuple[int, int, int, int] | None = None,
        full_resolution: bool = False,
        detail: bool = False,
    ) -> ImageReference:
        key = self._key(attachment_id, region, full_resolution, detail)
        metadata = root / "metadata" / f"{key}.json"
        if metadata.is_file():
            return ImageReference(**json.loads(metadata.read_text()))
        try:
            with Image.open(BytesIO(data)) as source:
                mime = _MIMES.get(source.format or "")
                if mime is None or getattr(source, "n_frames", 1) != 1:
                    raise ImageDeliveryError(
                        "Only static PNG, JPEG and WebP images are supported"
                    )
                if source.width * source.height > self.config.max_pixels:
                    raise ImageDeliveryError("Image exceeds the decoded pixel limit")
                orientation = source.getexif().get(274, 1)
                original = ImageOps.exif_transpose(source)
                original.load()
                original_width, original_height = original.size
                if region is not None:
                    x, y, width, height = region
                    if x + width > original.width or y + height > original.height:
                        raise ImageDeliveryError(
                            "Requested region is outside the original image"
                        )
                    original = original.crop((x, y, x + width, y + height))
                budget = (
                    (self.config.detail_max_base64_bytes // 4 * 3)
                    if detail
                    else self.config.normal_max_bytes
                )
                edge = self.config.max_edge_px
                passthrough = (
                    region is None and orientation in (None, 1) and len(data) <= budget
                )
                if passthrough and (full_resolution or max(original.size) <= edge):
                    encoded, sent_mime, size = data, mime, original.size
                else:
                    encoded, sent_mime, size = self._encode(
                        original, mime, budget, full_resolution
                    )
                image = ImageReference(
                    attachment_id,
                    sha256(encoded).hexdigest(),
                    sent_mime,
                    size[0],
                    size[1],
                    len(encoded),
                    original_width,
                    original_height,
                    name=Path(name).name[:200] or "image",
                )
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ImageDeliveryError("Invalid or unsafe image data") from error
        path = root / "variants" / image.variant_id
        if not path.exists():
            _atomic_write(path, encoded)
        _atomic_write(metadata, json.dumps(asdict(image), ensure_ascii=False).encode())
        return image

    def _encode(
        self, original: Image.Image, mime: str, budget: int, full_resolution: bool
    ) -> tuple[bytes, str, tuple[int, int]]:
        transparent = "A" in original.getbands() or "transparency" in original.info
        if original.mode not in {"RGB", "RGBA"}:
            original = original.convert("RGBA" if transparent else "RGB")
        edges = (
            [max(original.size)]
            if full_resolution
            else sorted(
                {
                    min(max(original.size), self.config.max_edge_px),
                    *(
                        value
                        for value in _EDGES
                        if value <= self.config.max_edge_px
                        and value < max(original.size)
                    ),
                },
                reverse=True,
            )
        )

        def encode(
            edge: int, format_: str, quality: int = 80
        ) -> tuple[bytes, str, tuple[int, int]]:
            image = original.copy()
            image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            output = BytesIO()
            if format_ == "PNG":
                image.save(output, format="PNG", optimize=True)
            else:
                image.convert("RGB").save(
                    output, format="JPEG", quality=quality, subsampling=0, optimize=True
                )
            return (
                output.getvalue(),
                "image/png" if format_ == "PNG" else "image/jpeg",
                image.size,
            )

        jpeg_edges = edges
        if mime != "image/jpeg" or transparent:
            png_edges = (
                edges
                if transparent or full_resolution
                else [edge for edge in edges if edge >= min(1000, edges[0])]
            )
            for edge in png_edges:
                candidate = encode(edge, "PNG")
                if len(candidate[0]) <= budget:
                    return candidate
            if transparent:
                raise ImageDeliveryError(
                    "Transparent image cannot fit the byte budget; crop a smaller region"
                )
            jpeg_edges = [edge for edge in edges if edge <= png_edges[-1]]
        for edge in jpeg_edges:
            for quality in (90,) if full_resolution else _QUALITIES:
                candidate = encode(edge, "JPEG", quality)
                if len(candidate[0]) <= budget:
                    return candidate
        raise ImageDeliveryError(
            "Image cannot fit the byte budget; crop a smaller region or increase the image budget"
        )

    def _sweep_originals(self, directory: Path) -> None:
        if not directory.exists():
            return
        entries = sorted(
            (
                (path.stat().st_mtime_ns, path, path.stat().st_size)
                for path in directory.iterdir()
                if path.is_file()
            ),
            key=lambda item: (item[0], item[1].name),
        )
        total = sum(size for _, _, size in entries)
        for _, path, size in entries:
            if total <= self.config.originals_cache_max_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size
