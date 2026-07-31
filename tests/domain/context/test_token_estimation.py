import hashlib
from io import BytesIO
import threading
import time

import reuleauxcoder.domain.context.manager as manager


def test_mixed_text_fallback_uses_weighted_estimate(monkeypatch) -> None:
    monkeypatch.setattr(manager, "_tiktoken_encoder", None)
    monkeypatch.setattr(manager, "_tiktoken_cache_path", lambda: None)
    message = {"role": "user", "content": "你好 world!"}

    tokens = manager.estimate_message_tokens(message)

    # Chinese: 2 × 1.5, English words: 1 × 1.3, symbols: 1 × 0.5.
    assert tokens == 5
    assert message[manager.MESSAGE_TOKEN_KEY] == 5


def test_fallback_ignores_whitespace_and_counts_non_english_symbols() -> None:
    assert manager._estimate_text_tokens_chars("hello world") == 2.6
    assert manager._estimate_text_tokens_chars("123 !?") == 2.5


def test_vocabulary_download_reports_percentage_and_writes_valid_cache(
    tmp_path, monkeypatch
) -> None:
    content = b"tokenizer vocabulary"

    class FakeResponse:
        headers = {"Content-Length": str(len(content))}

        def __init__(self) -> None:
            self._stream = BytesIO(content)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, size: int) -> bytes:
            return self._stream.read(size)

    monkeypatch.setattr(manager, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr(
        manager,
        "_TIKTOKEN_VOCABULARY_SHA256",
        hashlib.sha256(content).hexdigest(),
    )
    progress = []
    cache_path = tmp_path / "tokenizer-cache"

    manager._download_tiktoken_vocabulary(
        cache_path,
        progress=progress.append,
        cancelled=threading.Event(),
    )

    assert cache_path.read_bytes() == content
    assert "Downloading tokenizer vocabulary... 0%." in progress
    assert "Downloading tokenizer vocabulary... 100%." in progress


def test_slow_vocabulary_download_times_out_and_uses_estimate(
    tmp_path, monkeypatch
) -> None:
    def slow_download(_cache_path, *, progress, cancelled) -> None:
        del progress
        cancelled.wait(1)

    monkeypatch.setattr(manager, "_tiktoken_encoder", None)
    monkeypatch.setattr(manager, "_tiktoken_download_thread", None)
    monkeypatch.setattr(
        manager,
        "_tiktoken_cache_path",
        lambda: tmp_path / "missing-tokenizer-cache",
    )
    monkeypatch.setattr(manager, "_download_tiktoken_vocabulary", slow_download)
    progress = []
    started = time.monotonic()

    encoder = manager.prepare_tiktoken_encoder(
        progress=progress.append,
        timeout_seconds=0.1,
    )

    elapsed = time.monotonic() - started
    assert encoder is None
    assert elapsed < 0.5
    assert (
        "Tokenizer vocabulary download timed out after 0.1s; "
        "using estimated token counts."
    ) in progress
    thread = manager._tiktoken_download_thread
    assert thread is not None
    thread.join(timeout=1)


def test_missing_vocabulary_never_triggers_implicit_tiktoken_download(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(manager, "_tiktoken_encoder", None)
    monkeypatch.setattr(
        manager,
        "_tiktoken_cache_path",
        lambda: tmp_path / "missing-tokenizer-cache",
    )

    assert manager._get_tiktoken_encoder() is None
