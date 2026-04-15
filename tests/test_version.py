import importlib
from importlib.metadata import version


def test_package_version_matches_project_metadata() -> None:
    pkg = importlib.import_module("reuleauxcoder")
    assert pkg.__version__
    assert pkg.__version__ == version("reuleauxcoder")
