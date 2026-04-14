import sys

import pytest

from reuleauxcoder.interfaces.cli.args import parse_args


def test_parse_args_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["rcoder"])
    args = parse_args()
    assert args.config is None
    assert args.model is None
    assert args.prompt is None
    assert args.resume is None


def test_parse_args_all_supported_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["rcoder", "-c", "config.yaml", "-m", "gpt-4o", "-p", "hello", "-r", "session-1"],
    )
    args = parse_args()
    assert args.config == "config.yaml"
    assert args.model == "gpt-4o"
    assert args.prompt == "hello"
    assert args.resume == "session-1"


def test_parse_args_version_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["rcoder", "--version"])
    with pytest.raises(SystemExit):
        parse_args()
