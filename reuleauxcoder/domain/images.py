"""Image references and non-destructive, provider-neutral request projections."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import json
import re
from typing import Callable, Protocol


_HASH = re.compile(r"^[a-f0-9]{64}$")
IMAGE_TURN_KEY = "_rc_image_turn_id"


def input_modalities(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(item not in ("text", "image") for item in value)
        or "text" not in value
        or len(set(value)) != len(value)
    ):
        raise ValueError(
            "support_modal must be an array containing text and optionally image"
        )
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ImageConfig:
    max_edge_px: int = 2000
    normal_max_bytes: int = 256 * 1024
    detail_max_base64_bytes: int = 2 * 1024 * 1024
    originals_cache_max_bytes: int = 1024 * 1024 * 1024
    import_max_bytes: int = 64 * 1024 * 1024
    max_pixels: int = 40_000_000

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f"attachments.image.{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ImageReference:
    attachment_id: str
    variant_id: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    original_width: int
    original_height: int
    name: str = "image"
    turn_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attachment_id, str)
            or not isinstance(self.variant_id, str)
            or not _HASH.fullmatch(self.attachment_id)
            or not _HASH.fullmatch(self.variant_id)
        ):
            raise ValueError("Invalid image reference")
        if (
            not isinstance(self.name, str)
            or len(self.name) > 256
            or (self.turn_id is not None and not isinstance(self.turn_id, str))
        ):
            raise ValueError("Invalid image name or turn")
        if self.mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Unsupported image type")
        for value in (
            self.width,
            self.height,
            self.size_bytes,
            self.original_width,
            self.original_height,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("Invalid image dimensions or size")

    def to_part(self) -> dict:
        return {"type": "image", **asdict(self)}

    @classmethod
    def from_part(cls, part: dict) -> ImageReference:
        return cls(**{key: value for key, value in part.items() if key != "type"})

    def caption(self) -> str:
        return (
            f"[Image {self.attachment_id}: {self.name}; original "
            f"{self.original_width}x{self.original_height}; sent {self.width}x{self.height}, "
            f"{self.size_bytes} bytes. For unreadable detail use view_image with this "
            "attachment ID and a region in original, orientation-corrected pixels; "
            "original availability depends on the image cache.]"
        )


@dataclass(frozen=True, slots=True)
class ChatInput:
    text: str = ""
    images: tuple[ImageReference, ...] = ()
    session_id: str | None = None
    session_generation: int | None = None
    image_labels: tuple[str, ...] = ()
    submission_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not isinstance(self.images, tuple):
            raise ValueError("Invalid chat input")
        if any(not isinstance(image, ImageReference) for image in self.images):
            raise ValueError("Invalid chat image")
        if self.image_labels and (
            not isinstance(self.image_labels, tuple)
            or len(self.image_labels) != len(self.images)
            or any(
                not isinstance(label, str)
                or not re.fullmatch(r"\[Image #[1-9]\d*\]", label)
                for label in self.image_labels
            )
            or len(set(self.image_labels)) != len(self.image_labels)
            or any(self.text.count(label) != 1 for label in self.image_labels)
        ):
            raise ValueError(
                "Each image must have one distinct [Image #N] marker in the draft"
            )
        if self.session_id is not None and not isinstance(self.session_id, str):
            raise ValueError("Invalid input session")
        if (
            self.session_generation is not None
            and type(self.session_generation) is not int
        ):
            raise ValueError("Invalid input generation")

    @property
    def display_text(self) -> str:
        return display_content(self.content(""))

    def content(self, turn_id: str) -> str | list[dict]:
        if not self.images:
            return self.text
        if self.image_labels:
            parts = []
            cursor = 0
            for position, label, image in sorted(
                (self.text.index(label), label, image)
                for label, image in zip(self.image_labels, self.images)
            ):
                end = position + len(label)
                parts.append({"type": "text", "text": self.text[cursor:end]})
                parts.append(replace(image, turn_id=turn_id).to_part())
                cursor = end
            if cursor < len(self.text):
                parts.append({"type": "text", "text": self.text[cursor:]})
            return parts
        return [
            *([{"type": "text", "text": self.text}] if self.text else []),
            *(replace(image, turn_id=turn_id).to_part() for image in self.images),
        ]


def display_content(content: object) -> str:
    """Render user attachments as numbered labels, keeping inline text positions."""
    if not isinstance(content, list):
        return content_text(content)
    text = ""
    number = 0
    previous_text = False
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image":
            number += 1
            if not previous_text or not re.search(r"\[Image #[1-9]\d*\]$", text):
                text += ("\n" if text else "") + f"[Image #{number}]"
        elif part.get("type") == "text":
            text += part["text"]
        previous_text = part.get("type") == "text"
    return text


class ImageStorePort(Protocol):
    def data_url(self, session_id: str, image: ImageReference) -> str: ...
    def import_bytes(
        self, session_id: str, data: bytes, *, name: str = "image"
    ) -> ImageReference: ...
    def read_image(
        self,
        session_id: str,
        attachment_id: str,
        *,
        region: tuple[int, int, int, int] | None = None,
        full_resolution: bool = False,
    ) -> ImageReference: ...


def content_text(content: object) -> str:
    """A text projection for UI/search/summaries; never stringify binary data."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    result: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            result.append(str(part.get("text", "")))
        elif part.get("type") == "image":
            result.append(ImageReference.from_part(part).caption())
        elif part.get("type") in {"image_url", "input_image"}:
            result.append("[Image content; not included in this text projection]")
    return "\n".join(result)


def image_parts(messages: list[dict]) -> list[dict]:
    return [
        part
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if isinstance(part, dict) and part.get("type") in {"image", "image_url"}
    ]


def image_identity(part: dict) -> str:
    if part.get("type") == "image":
        return str(part["variant_id"])
    return sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()


@dataclass
class ImageRecovery:
    """A turn-scoped request view. It never writes placeholders to history."""

    level: int = 0
    stripped: set[str] = field(default_factory=set)

    def advance(self, messages: list[dict]) -> bool:
        parts = image_parts(messages)
        if not parts or self.level >= 2:
            return False
        if self.level == 0 and len(parts) > 2:
            self.level = 1
        else:
            self.level = 2
            self.stripped.update(image_identity(part) for part in parts)
        return True


def project_images(
    messages: list[dict],
    *,
    supports_images: bool,
    load_image: Callable[[ImageReference], str] | None = None,
    retention: str = "history",
    turn_id: str | None = None,
    recovery: ImageRecovery | None = None,
) -> list[dict]:
    """Project all roles, preserving positions and tool-result associations."""
    eligible = [
        part
        for part in image_parts(messages)
        if not (
            retention == "user_turn"
            and part.get("type") == "image"
            and part.get("turn_id") != turn_id
        )
    ]
    older = max(0, len(eligible) - 2) if recovery and recovery.level == 1 else 0
    result: list[dict] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            result.append(dict(message))
            continue
        projected: list[dict] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in {
                "image",
                "image_url",
            }:
                projected.append(dict(part) if isinstance(part, dict) else part)
                continue
            reason = None
            reference = (
                ImageReference.from_part(part) if part["type"] == "image" else None
            )
            if retention == "user_turn" and reference and reference.turn_id != turn_id:
                reason = "outside the current user turn"
            elif not supports_images:
                reason = "current model does not support image input"
            elif (
                recovery
                and recovery.level == 2
                and image_identity(part) in recovery.stripped
            ):
                reason = "provider rejected request size"
            elif older:
                older -= 1
                reason = (
                    "provider rejected request size; retaining the latest two images"
                )
            if reason:
                identity = (
                    reference.attachment_id if reference else image_identity(part)
                )
                projected.append(
                    {"type": "text", "text": f"[Image {identity} not sent: {reason}.]"}
                )
            elif reference:
                if load_image is None:
                    raise ValueError("Image store is unavailable")
                projected.extend(
                    [
                        {"type": "text", "text": reference.caption()},
                        {
                            "type": "image_url",
                            "image_url": {"url": load_image(reference)},
                        },
                    ]
                )
            else:
                projected.append(
                    {"type": "image_url", "image_url": dict(part["image_url"])}
                )
        result.append({**message, "content": projected})
    return result


def image_request_stats(messages: list[dict]) -> dict[str, int]:
    parts = image_parts(messages)
    return {
        "image_count": len(parts),
        "image_base64_bytes": sum(
            len(url.split(",", 1)[1])
            for part in parts
            if (url := part.get("image_url", {}).get("url", "")).startswith("data:")
            and "," in url
        ),
    }
