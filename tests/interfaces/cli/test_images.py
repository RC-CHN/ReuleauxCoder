import os

from reuleauxcoder.interfaces.cli.images import pasted_image_path
from tests.domain.test_images import picture


def test_pasted_paths_accept_quotes_urls_and_escapes_but_leave_text_alone(tmp_path):
    path = tmp_path / "screen shot.png"
    path.write_bytes(picture(size=(4, 3)))
    paths = [
        str(path),
        f'"{path}"',
        f"'{path}'",
        path.as_uri(),
    ]
    # Backslashes are path separators on Windows, not shell space escapes.
    if os.name != "nt":
        paths.append(str(path).replace(" ", "\\ "))
    for text in paths:
        assert pasted_image_path(text) == path
    for text in (
        "please inspect screenshot.png",
        str(path) + "\nother.png",
        str(tmp_path),
        str(tmp_path / "missing.png"),
    ):
        assert pasted_image_path(text) is None
