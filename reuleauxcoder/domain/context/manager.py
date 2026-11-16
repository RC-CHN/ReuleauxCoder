"""Context manager - manages conversation context and compression."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.request import urlopen
import uuid

from reuleauxcoder.domain.context.budget import ContextBudget
from reuleauxcoder.domain.images import content_text, image_parts
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.context.rounds import (
    group_api_rounds,
)
from reuleauxcoder.domain.context.summary import (
    CompactionError, CompactionRequest, generate_summary, summary_instruction,
)
from reuleauxcoder.domain.llm.errors import LLMRequestCancelled
from reuleauxcoder.domain.context.replay import align_item_provenance
from reuleauxcoder.domain.context.usage import UsageObservation
from reuleauxcoder.domain.llm.context_messages import (
    synthetic_user_message,
)

if TYPE_CHECKING:
    from reuleauxcoder.app.ui_events import UIEventBus

# Tiktoken's public encoding constructor downloads this vocabulary on its first
# cache miss. Its downloader has no timeout, so never call it until we have put
# a validated local copy in the cache ourselves.
_TIKTOKEN_ENCODING_NAME = "o200k_base"
_TIKTOKEN_VOCABULARY_URL = (
    "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
)
_TIKTOKEN_VOCABULARY_SHA256 = (
    "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
)
_TIKTOKEN_DOWNLOAD_TIMEOUT_SECONDS = 5.0
_TIKTOKEN_SOCKET_TIMEOUT_SECONDS = _TIKTOKEN_DOWNLOAD_TIMEOUT_SECONDS
_TIKTOKEN_DOWNLOAD_CHUNK_SIZE = 64 * 1024

_tiktoken_encoder = None
_tiktoken_download_lock = threading.Lock()
_tiktoken_download_thread: threading.Thread | None = None
MESSAGE_TOKEN_KEY = "_rc_token_count"

_CJK_CHARACTER_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\U00020000-\U0002ebef]"
)
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")


class _TokenizerDownloadCancelled(Exception):
    """Internal signal used to abandon a timed-out background download."""


def _tiktoken_cache_path() -> Path | None:
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        cache_dir = os.environ["TIKTOKEN_CACHE_DIR"]
    elif "DATA_GYM_CACHE_DIR" in os.environ:
        cache_dir = os.environ["DATA_GYM_CACHE_DIR"]
    else:
        cache_dir = str(Path(tempfile.gettempdir()) / "data-gym-cache")
    if not cache_dir:
        return None
    cache_key = hashlib.sha1(_TIKTOKEN_VOCABULARY_URL.encode()).hexdigest()
    return Path(cache_dir) / cache_key


def _has_valid_tiktoken_vocabulary(path: Path) -> bool:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == _TIKTOKEN_VOCABULARY_SHA256


def has_cached_tiktoken_vocabulary() -> bool:
    """Return whether the pinned vocabulary is already valid on disk."""
    cache_path = _tiktoken_cache_path()
    return cache_path is not None and _has_valid_tiktoken_vocabulary(cache_path)


def token_count_backend_name() -> str:
    """Describe the backend used by the most recent local token count."""
    if _tiktoken_encoder is not None:
        return f"tiktoken/{_TIKTOKEN_ENCODING_NAME}"
    return "weighted estimate"


def _get_tiktoken_encoder():
    """Load the modern tokenizer only when its vocabulary is already cached."""
    global _tiktoken_encoder
    if _tiktoken_encoder is not None:
        return _tiktoken_encoder

    cache_path = _tiktoken_cache_path()
    if cache_path is None or not _has_valid_tiktoken_vocabulary(cache_path):
        return None
    try:
        import tiktoken

        _tiktoken_encoder = tiktoken.get_encoding(_TIKTOKEN_ENCODING_NAME)
    except Exception:
        _tiktoken_encoder = None
    return _tiktoken_encoder


def _safe_tokenizer_progress(
    progress: Callable[[str], None] | None,
    message: str,
) -> None:
    if progress is None:
        return
    try:
        progress(message)
    except Exception:
        pass


def _download_tiktoken_vocabulary(
    cache_path: Path,
    *,
    progress: Callable[[str], None] | None,
    cancelled: threading.Event,
) -> None:
    """Stream and validate the vocabulary before atomically caching it."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    digest = hashlib.sha256()
    downloaded = 0
    last_percent = 0

    def report(message: str) -> None:
        if not cancelled.is_set():
            _safe_tokenizer_progress(progress, message)

    try:
        with urlopen(  # noqa: S310 - fixed HTTPS URL with a pinned content hash
            _TIKTOKEN_VOCABULARY_URL,
            timeout=_TIKTOKEN_SOCKET_TIMEOUT_SECONDS,
        ) as response:
            content_length = response.headers.get("Content-Length")
            try:
                total_bytes = int(content_length) if content_length else 0
            except ValueError:
                total_bytes = 0
            if total_bytes:
                total_mb = total_bytes / (1024 * 1024)
                report(
                    "Downloading tokenizer vocabulary... "
                    f"0% (0.0/{total_mb:.1f} MB)."
                )
            else:
                report("Downloading tokenizer vocabulary... 0%.")
            with temporary.open("wb") as stream:
                while True:
                    if cancelled.is_set():
                        raise _TokenizerDownloadCancelled
                    chunk = response.read(_TIKTOKEN_DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    if cancelled.is_set():
                        raise _TokenizerDownloadCancelled
                    stream.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if total_bytes:
                        percent = min(100, int(downloaded * 100 / total_bytes))
                        if percent >= last_percent + 10:
                            report(
                                "Downloading tokenizer vocabulary... "
                                f"{percent}% "
                                f"({downloaded / (1024 * 1024):.1f}/"
                                f"{total_mb:.1f} MB)."
                            )
                            last_percent = percent
                    elif downloaded // (1024 * 1024) > (
                        downloaded - len(chunk)
                    ) // (1024 * 1024):
                        report(
                            "Downloading tokenizer vocabulary... "
                            f"{downloaded / (1024 * 1024):.1f} MB."
                        )
        if cancelled.is_set():
            raise _TokenizerDownloadCancelled
        if digest.hexdigest() != _TIKTOKEN_VOCABULARY_SHA256:
            raise ValueError("downloaded tokenizer vocabulary failed hash validation")
        temporary.replace(cache_path)
        if last_percent < 100:
            report("Downloading tokenizer vocabulary... 100%.")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _is_timeout_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, URLError) and isinstance(
        getattr(error, "reason", None), (TimeoutError, socket.timeout)
    ):
        return True
    return False


def prepare_tiktoken_encoder(
    *,
    progress: Callable[[str], None] | None = None,
    timeout_seconds: float = _TIKTOKEN_DOWNLOAD_TIMEOUT_SECONDS,
):
    """Prepare tiktoken within a deadline, or return None for heuristic use."""
    global _tiktoken_download_thread

    started = time.monotonic()
    encoder = _get_tiktoken_encoder()
    if encoder is not None:
        return encoder

    cache_path = _tiktoken_cache_path()
    if cache_path is None:
        _safe_tokenizer_progress(
            progress,
            "Tokenizer cache is disabled; using estimated token counts.",
        )
        return None

    timeout_seconds = max(0.0, float(timeout_seconds))
    with _tiktoken_download_lock:
        encoder = _get_tiktoken_encoder()
        if encoder is not None:
            return encoder
        if (
            _tiktoken_download_thread is not None
            and _tiktoken_download_thread.is_alive()
        ):
            _safe_tokenizer_progress(
                progress,
                "Tokenizer vocabulary is still downloading; "
                "using estimated token counts.",
            )
            return None

        _safe_tokenizer_progress(
            progress,
            "Tokenizer vocabulary is not cached; starting download...",
        )
        cancelled = threading.Event()
        errors: list[Exception] = []

        def download() -> None:
            try:
                _download_tiktoken_vocabulary(
                    cache_path,
                    progress=progress,
                    cancelled=cancelled,
                )
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(
            target=download,
            name="rcoder-tokenizer-download",
            daemon=True,
        )
        _tiktoken_download_thread = thread
        thread.start()
        thread.join(timeout_seconds)
        if thread.is_alive():
            cancelled.set()
            _safe_tokenizer_progress(
                progress,
                f"Tokenizer vocabulary download timed out after "
                f"{timeout_seconds:.1f}s; using estimated token counts.",
            )
            return None
        _tiktoken_download_thread = None

        if errors:
            error = errors[0]
            if _is_timeout_error(error):
                _safe_tokenizer_progress(
                    progress,
                    f"Tokenizer vocabulary download timed out after "
                    f"{timeout_seconds:.1f}s; using estimated token counts.",
                )
            else:
                _safe_tokenizer_progress(
                    progress,
                    "Tokenizer vocabulary download failed "
                    f"({type(error).__name__}, "
                    f"{time.monotonic() - started:.1f}s); "
                    "using estimated token counts.",
                )
            return None

        encoder = _get_tiktoken_encoder()
        if encoder is None:
            _safe_tokenizer_progress(
                progress,
                "Tokenizer vocabulary could not be loaded; "
                "using estimated token counts.",
            )
            return None
        try:
            vocabulary_mb = cache_path.stat().st_size / (1024 * 1024)
        except OSError:
            vocabulary_mb = 0.0
        _safe_tokenizer_progress(
            progress,
            f"Tokenizer vocabulary ready ({vocabulary_mb:.1f} MB, "
            f"{time.monotonic() - started:.1f}s).",
        )
        return encoder


def _estimate_text_tokens_chars(text: str) -> float:
    """Estimate mixed text with a floor for long unbroken ASCII runs."""
    chinese_characters = len(_CJK_CHARACTER_RE.findall(text))
    without_chinese = _CJK_CHARACTER_RE.sub("", text)
    english_tokens = sum(
        max(1.3, len(match.group()) / 4)
        for match in _ENGLISH_WORD_RE.finditer(without_chinese)
    )
    remaining = _ENGLISH_WORD_RE.sub("", without_chinese)
    other_symbols = sum(1 for character in remaining if not character.isspace())
    return (
        chinese_characters * 1.5
        + english_tokens
        + other_symbols * 0.5
    )


def _estimate_message_tokens_chars(message: dict) -> int:
    """Estimate a message without requiring a tokenizer vocabulary."""
    total = 0.0
    if message.get("content"):
        total += _estimate_text_tokens_chars(content_text(message["content"]))
        total += 2048 * len(image_parts([message]))
    if message.get("tool_calls"):
        total += _estimate_text_tokens_chars(str(message["tool_calls"]))
    return math.ceil(total)


def estimate_message_tokens(
    message: dict, *, refresh: bool = False, token_fudge_factor: float = 1.1
) -> int:
    """Estimate token count for a single message and cache it on the message."""
    cached = message.get(MESSAGE_TOKEN_KEY)
    if not refresh and isinstance(cached, int):
        return cached

    encoder = _get_tiktoken_encoder()
    if encoder is None:
        total = _estimate_message_tokens_chars(message)
    else:
        total = 0
        if message.get("content"):
            try:
                total += len(encoder.encode(content_text(message["content"])))
            except Exception:
                total += len(content_text(message["content"])) // 3
            # Initial provider-neutral estimate; measured usage calibrates it.
            # Base64 length measures traffic, not vision tokens.
            total += 2048 * len(image_parts([message]))
        if message.get("tool_calls"):
            try:
                total += len(encoder.encode(str(message["tool_calls"])))
            except Exception:
                total += len(str(message["tool_calls"])) // 3
        total = int(total * token_fudge_factor)

    message[MESSAGE_TOKEN_KEY] = total
    return total


def ensure_message_token_counts(
    messages: list[dict], *, refresh: bool = False, token_fudge_factor: float = 1.1
) -> int:
    """Ensure messages have cached token counts and return the total."""
    total = 0
    for message in messages:
        total += estimate_message_tokens(
            message, refresh=refresh, token_fudge_factor=token_fudge_factor
        )
    return total


def estimate_tokens(messages: list[dict], token_fudge_factor: float = 1.1) -> int:
    """Estimate token count for messages using cached message counts."""
    return ensure_message_token_counts(messages, token_fudge_factor=token_fudge_factor)


class ContextManager:
    """Plan one bounded prefix checkpoint from complete request pressure."""

    def __init__(
        self, max_tokens: int = 128_000, ui_bus: "UIEventBus | None" = None,
        auto_compact: bool = True, threshold_ratio: float = 0.8,
        retain_ratio: float = 0.16, summary_max_tokens: int = 8_192,
        compaction_retries: int = 1, max_overflow_retries: int = 1,
        token_fudge_factor: float = 1.1, reserved_output_tokens: int = 8_192,
        fixed_prompt_tokens: int = 0, tool_schema_tokens: int = 0,
        safety_margin_tokens: int = 2_048,
    ):
        self.max_tokens = max_tokens
        self._ui_bus = ui_bus
        self._auto_compact = auto_compact
        self.threshold_ratio = threshold_ratio
        self.retain_ratio = retain_ratio
        self.summary_max_tokens = summary_max_tokens
        self.compaction_retries = compaction_retries
        self.max_overflow_retries = max_overflow_retries
        self._token_fudge_factor = token_fudge_factor
        self._budget = ContextBudget(
            model_window=max_tokens, reserved_output=reserved_output_tokens,
            fixed_prompt_tokens=fixed_prompt_tokens, tool_schema_tokens=tool_schema_tokens,
            safety_margin=safety_margin_tokens,
        )
        self._history_version = 0
        self._cache_epoch = 0
        self._checkpoints: list[CompactionCheckpoint] = []
        self._usage_observations: list[UsageObservation] = []
        self._latest_usage: UsageObservation | None = None
        self._estimate_scale_by_profile: dict[str, float] = {}
        self._active_profile = "default"
        self._request_overhead = fixed_prompt_tokens + tool_schema_tokens
        self.last_error: str | None = None
        self.image_projection: Callable[[list[dict]], list[dict]] | None = None

    def get_context_tokens(self, messages: list[dict]) -> int:
        if self.image_projection is not None and image_parts(messages):
            messages = self.image_projection(messages)
            for message in messages:
                if image_parts([message]):
                    message.pop(MESSAGE_TOKEN_KEY, None)
        return estimate_tokens(messages, token_fudge_factor=self._token_fudge_factor)

    def reconfigure(self, max_tokens: int, *, auto_compact: bool | None = None) -> None:
        from dataclasses import replace
        self.max_tokens = max_tokens
        if auto_compact is not None:
            self._auto_compact = auto_compact
        self._budget = replace(self._budget, model_window=max_tokens)
        self._latest_usage = None

    def restore_replay_state(self, *, history_version: int, cache_epoch: int) -> None:
        self._history_version = max(0, int(history_version))
        self._cache_epoch = max(0, int(cache_epoch))
        self._latest_usage = None

    def invalidate_replay_prefix(self) -> None:
        self._history_version += 1
        self._cache_epoch += 1
        self._latest_usage = None

    def input_limit(self, output_tokens: int = 0) -> int:
        return max(0, self.max_tokens - max(output_tokens, self._budget.reserved_output)
                   - self._budget.safety_margin)

    def pressure_threshold(self, output_tokens: int = 0) -> int:
        return min(int(self.max_tokens * self.threshold_ratio), self.input_limit(output_tokens))

    @property
    def request_input_limit(self) -> int:
        return self.input_limit()

    @property
    def rewrite_thresholds(self) -> dict[str, int]:
        return {"threshold": self.pressure_threshold(),
                "retain": int(max(0, self.max_tokens - self._budget.reserved_output) * self.retain_ratio)}

    @property
    def automatic_strategies(self) -> tuple[str, ...]:
        return ("compact",) if self._auto_compact else ()

    @property
    def automatic_strategy_settings(self) -> dict[str, bool]:
        return {"auto_compact": self._auto_compact}

    @property
    def history_version(self) -> int:
        return self._history_version

    @property
    def cache_epoch(self) -> int:
        return self._cache_epoch

    @property
    def checkpoints(self) -> tuple[CompactionCheckpoint, ...]:
        return tuple(self._checkpoints)

    def restore_checkpoints(self, checkpoints) -> None:
        self._checkpoints = list(checkpoints)

    def clear_usage_observations(self) -> None:
        self._usage_observations.clear()
        self._latest_usage = None
        self._estimate_scale_by_profile.clear()
        self._active_profile = "default"
        self._request_overhead = self._budget.fixed_prompt_tokens + self._budget.tool_schema_tokens

    @property
    def latest_usage(self) -> UsageObservation | None:
        return self._latest_usage

    def estimate_request_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        import json
        tool_tokens = _estimate_text_tokens_chars(json.dumps(tools, ensure_ascii=False)) if tools else 0
        return max(1, self.get_context_tokens(messages) + math.ceil(tool_tokens) + len(messages) * 4)

    def observe_usage(self, *, actual_prompt_tokens: int, cached_input_tokens: int | None,
                      local_request_estimate: int, local_history_estimate: int,
                      request_boundary: str, model_profile: str) -> UsageObservation | None:
        if actual_prompt_tokens <= 0:
            return None
        observation = UsageObservation.create(
            actual_prompt_tokens=actual_prompt_tokens, cached_input_tokens=cached_input_tokens,
            local_request_estimate=local_request_estimate, local_history_estimate=local_history_estimate,
            history_version=self._history_version, request_boundary=request_boundary,
            model_profile=model_profile,
        )
        ratio = min(3.0, max(0.5, observation.actual_prompt_tokens / max(1, local_request_estimate)))
        previous = self._estimate_scale_by_profile.get(model_profile)
        self._estimate_scale_by_profile[model_profile] = ratio if previous is None else previous * .75 + ratio * .25
        self._active_profile = model_profile
        self._request_overhead = max(0, local_request_estimate - local_history_estimate)
        self._latest_usage = observation
        self._usage_observations.append(observation)
        del self._usage_observations[:-100]
        return observation

    def predict_request_tokens(self, messages: list[dict], tools: list[dict] | None = None,
                               *, complete: bool = False, local_estimate: int | None = None) -> int:
        local = local_estimate if local_estimate is not None else (
            self.estimate_request_tokens(messages, tools) if complete else self.get_context_tokens(messages)
        )
        observation = self._latest_usage
        scale = self._estimate_scale_by_profile.get(self._active_profile, 1.0)
        if observation is not None and observation.history_version == self._history_version:
            baseline = observation.local_request_estimate if complete else observation.local_history_estimate
            return max(1, int(observation.actual_prompt_tokens + (local - baseline) * scale))
        return max(1, int((local + (0 if complete else self._request_overhead)) * scale))

    def _select_prefix(self, messages: list[dict], request: CompactionRequest, retain: int) -> int:
        rounds = group_api_rounds(messages)
        if len(rounds) < 2:
            return 0
        accumulated = 0
        split = len(messages)
        for round_ in reversed(rounds):
            split -= len(round_.messages)
            projected = [request.messages[p] for p in request.history_positions[split:split + len(round_.messages)]]
            accumulated += self.get_context_tokens(projected)
            if accumulated >= retain:
                break
        # A summary must itself fit. Shorten the selected prefix only at complete
        # round boundaries; oversized recent batches remain available to the caller.
        boundaries = []
        offset = 0
        for round_ in rounds:
            offset += len(round_.messages)
            if offset <= split:
                boundaries.append(offset)
        for boundary in reversed(boundaries):
            summary_input = [*request.prefix(boundary), summary_instruction()]
            if self.predict_request_tokens(summary_input, request.tools, complete=True) <= self.input_limit(self.summary_max_tokens):
                return boundary
        return 0

    def maybe_compress(self, messages: list[dict], llm=None, *, request: CompactionRequest,
                       history_events=(), cancellation_event=None, trigger="pressure",
                       on_summary=None, remaining_tokens=None, before_commit=None) -> bool:
        if not self._auto_compact:
            return False
        return self._compact(messages, llm, request=request, history_events=history_events,
                             cancellation_event=cancellation_event, trigger=trigger,
                             on_summary=on_summary, remaining_tokens=remaining_tokens,
                             before_commit=before_commit)

    def force_compress(self, messages: list[dict], strategy: str, llm=None, **kwargs) -> bool:
        if strategy != "summarize":
            raise ValueError("Use /compact; snip and collapse have been replaced by prefix compaction.")
        return self._compact(messages, llm, trigger="manual", **kwargs)

    def _compact(self, messages: list[dict], llm, *, request: CompactionRequest,
                 history_events=(), cancellation_event=None, trigger="pressure",
                 on_summary=None, remaining_tokens=None, before_commit=None) -> bool:
        self.last_error = None
        before = self.predict_request_tokens(request.messages, request.tools, complete=True)
        threshold = self.pressure_threshold(request.output_tokens)
        if trigger == "pressure" and before < threshold:
            return False
        if llm is None:
            raise CompactionError("Compaction requires a model; history was retained.")
        if len(request.history_positions) != len(messages):
            raise CompactionError("Request history mapping is stale.")
        candidate = deepcopy(messages)
        current = request
        total_removed = 0
        generated = []
        retain = int(max(0, self.max_tokens - max(request.output_tokens, self._budget.reserved_output)) * self.retain_ratio)
        if trigger == "context-overflow":
            retain = 0
        started = False
        try:
            for _ in range(1 + self.compaction_retries):
                if cancellation_event is not None and cancellation_event.is_set():
                    raise LLMRequestCancelled("Context compaction cancelled")
                split = self._select_prefix(candidate, current, retain)
                if split == 0:
                    if not generated:
                        return False
                    raise CompactionError("No complete prefix can reduce the remaining request below its threshold.")
                if not started:
                    self._emit("before", trigger, before, before, len(messages), len(messages))
                    started = True
                prefix = current.prefix(split)
                output_limit = self.summary_max_tokens
                if remaining_tokens is not None:
                    remaining = remaining_tokens()
                    if remaining is not None:
                        output_limit = min(output_limit, remaining - self.estimate_request_tokens(
                            [*prefix, summary_instruction()], current.tools))
                        if output_limit <= 0:
                            raise CompactionError("Token budget exhausted before context summarization.")
                response = generate_summary(
                    prefix, llm, tools=current.tools, max_output_tokens=output_limit,
                    session_id=current.session_id, metadata=current.metadata,
                    cancellation_event=cancellation_event, on_response=on_summary,
                )
                summary = synthetic_user_message(
                    "context_summary", response.content, source="context_compactor",
                    attributes={"kind": "checkpoint"},
                )
                replacement = [summary, *candidate[split:]]
                updated = current.replace_history(replacement, split)
                previous_tokens = self.predict_request_tokens(current.messages, current.tools, complete=True)
                after = self.predict_request_tokens(updated.messages, updated.tools, complete=True)
                if after >= previous_tokens:
                    raise CompactionError("Summary did not shrink the complete request; history was retained.")
                candidate, current = replacement, updated
                total_removed += split - (1 if generated else 0)
                generated.append(response)
                if trigger == "manual" or after < threshold:
                    break
            else:
                raise CompactionError("Context remains above its threshold after bounded compaction attempts.")
            if cancellation_event is not None and cancellation_event.is_set():
                raise LLMRequestCancelled("Context compaction cancelled")
            if before_commit is not None:
                before_commit()
        except BaseException as error:
            self.last_error = str(error)
            if started:
                self._emit("failed", trigger, before, before, len(messages), len(messages), error=str(error))
            raise

        # All intermediate summaries stay private until the whole replacement is useful.
        sources = align_item_provenance(messages[:total_removed], history_events)
        source_ids = tuple(dict.fromkeys(ref for item in sources for ref in item["source_event_ids"]))
        source_checkpoints = tuple(dict.fromkeys(
            item["checkpoint_id"] for item in sources if item.get("checkpoint_id")
        ))
        observation = self._latest_usage
        checkpoint = CompactionCheckpoint.create(
            trigger=trigger, strategy=["prefix_checkpoint"],
            source_history_version=self._history_version, replacement_history=candidate,
            tokens_before=before, tokens_after=after,
            preserved_rounds=len(group_api_rounds(candidate[1:])),
            cache_epoch=self._cache_epoch + 1,
            actual_prompt_tokens=observation.actual_prompt_tokens if observation else None,
            cached_input_tokens=observation.cached_input_tokens if observation else None,
            reclaimed_tokens=before - after, source_event_ids=source_ids,
            source_checkpoint_ids=source_checkpoints,
            summary_prompt_tokens=sum(r.prompt_tokens for r in generated),
            summary_completion_tokens=sum(r.completion_tokens for r in generated),
            summary_cached_input_tokens=(sum(r.cached_input_tokens for r in generated)
                                         if all(r.cached_input_tokens is not None for r in generated)
                                         else None),
        )
        messages[:] = candidate
        self._history_version += 1
        self._cache_epoch += 1
        self._checkpoints.append(checkpoint)
        self._latest_usage = None
        self._emit("after", trigger, before, after,
                   len(request.history_positions), len(messages))
        return True

    def _emit(self, phase: str, trigger: str, before: int, after: int,
              before_count: int, after_count: int, *, error: str | None = None) -> None:
        if self._ui_bus is None:
            return
        from reuleauxcoder.domain.runtime.notification_kind import UIEventKind
        message = (
            f"Context compaction: {before:,} → {after:,} estimated request tokens."
            if phase == "after" else
            f"Context compaction failed: {error}" if phase == "failed" else
            f"Compacting context ({before:,} estimated request tokens)."
        )
        emit = self._ui_bus.warning if phase == "failed" else self._ui_bus.info
        emit(message, kind=UIEventKind.CONTEXT, phase=phase, trigger=trigger,
             before_tokens=before, after_tokens=after, before_message_count=before_count,
             after_message_count=after_count, token_delta=after - before,
             cache_epoch=self._cache_epoch, thresholds=self.rewrite_thresholds,
             applied_layers=["prefix_checkpoint"] if phase == "after" else [])
