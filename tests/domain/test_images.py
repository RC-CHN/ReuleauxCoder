from io import BytesIO

from PIL import Image
import pytest

from reuleauxcoder.domain.images import ImageConfig
from reuleauxcoder.infrastructure.persistence.images import (
    ImageDeliveryError,
    ImageStore,
)


def picture(size=(3000, 1600), color="white", format_="PNG"):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=format_)
    return output.getvalue()


def test_saved_variant_survives_original_eviction_and_restart(tmp_path):
    original = picture()
    config = ImageConfig(originals_cache_max_bytes=len(original))
    store = ImageStore(tmp_path, config)
    first = store.import_bytes("session", original, name="screen.png")
    wire = store.data_url("session", first)
    assert first.width == 2000
    assert first.size_bytes <= 256 * 1024
    store.import_bytes("session", picture(color="red"))
    restored = ImageStore(tmp_path, config)
    assert restored.data_url("session", first) == wire
    with pytest.raises(ImageDeliveryError, match="evicted"):
        restored.read_image("session", first.attachment_id, region=(0, 0, 100, 100))


def test_region_uses_original_pixels_and_larger_budget(tmp_path):
    store = ImageStore(tmp_path)
    image = store.import_bytes("session", picture())
    detail = store.read_image(
        "session",
        image.attachment_id,
        region=(2000, 1000, 1000, 600),
        full_resolution=True,
    )
    assert (detail.width, detail.height) == (1000, 600)
    assert (detail.original_width, detail.original_height) == (3000, 1600)
    assert len(store.data_url("session", detail).split(",")[1]) <= 2 * 1024 * 1024
    with pytest.raises(ImageDeliveryError, match="outside"):
        store.read_image("session", image.attachment_id, region=(2999, 0, 10, 10))


def test_bad_input_and_unattainable_budget_do_not_fall_back_to_original(tmp_path):
    store = ImageStore(tmp_path, ImageConfig(normal_max_bytes=1))
    with pytest.raises(ImageDeliveryError):
        store.import_bytes("session", picture())
    with pytest.raises(ImageDeliveryError):
        store.import_bytes("session", b"not an image")
    assert not (tmp_path / "session" / "images" / "variants").exists()
