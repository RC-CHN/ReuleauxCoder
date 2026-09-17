"""Recognize pasted local image paths while preserving ordinary text."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
from urllib.parse import urlsplit
from urllib.request import url2pathname


@dataclass(frozen=True)
class ImagePaste:
    path: Path
    text: str


def pasted_image_path(text: str) -> Path | None:
    text = text.strip()
    if not text or len(text) > 4096 or any(char in text for char in "\n\r\0"):
        return None
    unquoted = (
        text[1:-1]
        if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'"
        else text
    )
    try:
        if unquoted.startswith("file://"):
            url = urlsplit(unquoted)
            if url.netloc not in ("", "localhost") and os.name != "nt":
                return None
            path = url2pathname(
                ("//" + url.netloc if url.netloc and url.netloc != "localhost" else "")
                + url.path
            )
        elif re.match(r"^[a-zA-Z]:[\\/]", unquoted) or unquoted.startswith("\\\\"):
            path = unquoted
            if (
                os.name != "nt"
                and (os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"))
                and not path.startswith("\\\\")
            ):
                path = f"/mnt/{path[0].lower()}/" + path[3:].replace("\\", "/")
        else:
            parts = shlex.split(text)
            path = parts[0] if len(parts) == 1 else unquoted
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            return None
        with candidate.open("rb") as stream:
            header = stream.read(12)
        if (
            header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
            or header[:4] == b"RIFF"
            and header[8:12] == b"WEBP"
        ):
            return candidate
    except (OSError, ValueError):
        pass
    return None
