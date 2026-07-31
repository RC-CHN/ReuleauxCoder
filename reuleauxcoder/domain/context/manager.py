"""Context manager - manages conversation context and compression."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import math
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Optional, Any
from urllib.error import URLError
from urllib.request import urlopen
import uuid

from reuleauxcoder.domain.context.budget import ContextBudget
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.context.rounds import (
    group_api_rounds,
    normalize_history,
    recent_round_start,
)
from reuleauxcoder.domain.context.summary import CheckpointKind, generate_summary
from reuleauxcoder.domain.context.provider import ProviderContextCompactor
from reuleauxcoder.domain.context.usage import UsageObservation
from reuleauxcoder.domain.llm.context_messages import (
    is_synthetic_context_message,
    synthetic_user_message,
)

if TYPE_CHECKING:
    from reuleauxcoder.services.llm.client import LLM
    from reuleauxcoder.interfaces.events import UIEventBus

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
    """Estimate mixed text: CJK×1.5 + English words×1.3 + symbols×0.5."""
    chinese_characters = len(_CJK_CHARACTER_RE.findall(text))
    without_chinese = _CJK_CHARACTER_RE.sub("", text)
    english_words = len(_ENGLISH_WORD_RE.findall(without_chinese))
    remaining = _ENGLISH_WORD_RE.sub("", without_chinese)
    other_symbols = sum(1 for character in remaining if not character.isspace())
    return (
        chinese_characters * 1.5
        + english_words * 1.3
        + other_symbols * 0.5
    )


def _estimate_message_tokens_chars(message: dict) -> int:
    """Estimate a message without requiring a tokenizer vocabulary."""
    total = 0.0
    if message.get("content"):
        total += _estimate_text_tokens_chars(str(message["content"]))
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
                total += len(encoder.encode(str(message["content"])))
            except Exception:
                total += len(str(message["content"])) // 3
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


def estimate_tokens_tiktoken(
    messages: list[dict], token_fudge_factor: float = 1.1
) -> int:
    """Estimate token count using per-message cached counts with tiktoken fallback."""
    return ensure_message_token_counts(messages, token_fudge_factor=token_fudge_factor)


def estimate_tokens_chars(messages: list[dict]) -> int:
    """Estimate token count using chars/3 (fallback)."""
    total = 0
    for m in messages:
        total += _estimate_message_tokens_chars(m)
    return total


def estimate_tokens(messages: list[dict], token_fudge_factor: float = 1.1) -> int:
    """Estimate token count for messages using cached message counts."""
    return ensure_message_token_counts(messages, token_fudge_factor=token_fudge_factor)


SUMMARY_SYSTEM_PROMPT = """\
Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like file names, full code snippets, function signatures, file edits, etc.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
5. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
6. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
7. Optional Next Step: List the next step that you will take that is related to the most recent work you were working on. IMPORTANT: ensure that this step is DIRECTLY in line with the user's explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests without confirming with the user first.
8. If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure that there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

5. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

6. Current Work:
   [Precise description of current work]

7. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.
"""


class ContextManager:
    """Manages conversation context with multi-layer compression."""

    def __init__(
        self,
        max_tokens: int = 128_000,
        ui_bus: "UIEventBus | None" = None,
        snip_keep_recent_tools: int = 2,
        snip_threshold_chars: int = 1500,
        snip_min_lines: int = 6,
        summarize_keep_recent_turns: int = 5,
        token_fudge_factor: float = 1.1,
        reserved_output_tokens: int = 8_192,
        fixed_prompt_tokens: int = 0,
        tool_schema_tokens: int = 0,
        safety_margin_tokens: int = 2_048,
        provider_compactor: ProviderContextCompactor | None = None,
    ):
        self.max_tokens = max_tokens
        self._ui_bus = ui_bus
        # Snip configuration
        self._snip_keep_recent_tools = snip_keep_recent_tools
        self._snip_threshold_chars = snip_threshold_chars
        self._snip_min_lines = snip_min_lines
        # Summarize configuration
        self._summarize_keep_recent_turns = summarize_keep_recent_turns
        # Token fudge factor for safety margin
        self._token_fudge_factor = token_fudge_factor
        self._budget = ContextBudget(
            model_window=max_tokens,
            reserved_output=reserved_output_tokens,
            fixed_prompt_tokens=fixed_prompt_tokens,
            tool_schema_tokens=tool_schema_tokens,
            safety_margin=safety_margin_tokens,
        )
        self._recompute_thresholds()
        self._history_version = 0
        self._checkpoints: list[CompactionCheckpoint] = []
        self._consecutive_summary_failures = 0
        self._provider_compactor = provider_compactor
        # Rewrite/cache state
        self._cache_epoch = 0
        self._usage_observations: list[UsageObservation] = []
        self._latest_usage: UsageObservation | None = None
        self._estimate_scale_by_profile: dict[str, float] = {}

    def get_context_tokens(self, messages: list[dict]) -> int:
        """Get current locally-estimated context token count."""
        return estimate_tokens(messages, token_fudge_factor=self._token_fudge_factor)

    def reconfigure(self, max_tokens: int) -> None:
        """Update context budget and recompute layer thresholds."""
        self.max_tokens = max_tokens
        self._budget = ContextBudget(
            model_window=max_tokens,
            reserved_output=self._budget.reserved_output,
            fixed_prompt_tokens=self._budget.fixed_prompt_tokens,
            tool_schema_tokens=self._budget.tool_schema_tokens,
            safety_margin=self._budget.safety_margin,
        )
        self._recompute_thresholds()

    def restore_replay_state(self, *, history_version: int, cache_epoch: int) -> None:
        """Restore committed version watermarks without regenerating history."""
        self._history_version = max(0, int(history_version))
        self._cache_epoch = max(0, int(cache_epoch))
        self._latest_usage = None

    def invalidate_replay_prefix(self) -> None:
        """Start a new committed epoch after reset or stable-prefix replacement."""
        self._history_version += 1
        self._cache_epoch += 1
        self._latest_usage = None

    def _recompute_thresholds(self) -> None:
        limit = self._budget.request_input_limit
        self._snip_wall = max(1, int(limit * 0.60))
        self._semantic_wall = max(1, int(limit * 0.75))
        self._snip_min_gain = max(1, int(limit * 0.20))
        self._rewrite_target = max(1, int(limit * 0.40))
        self._emergency_at = max(1, int(limit * 0.90))

    @property
    def effective_input_tokens(self) -> int:
        return self._budget.available_input

    @property
    def request_input_limit(self) -> int:
        return self._budget.request_input_limit

    @property
    def rewrite_thresholds(self) -> dict[str, int]:
        return {
            "snip_wall": self._snip_wall,
            "semantic_wall": self._semantic_wall,
            "snip_min_gain": self._snip_min_gain,
            "rewrite_target": self._rewrite_target,
            "emergency_at": self._emergency_at,
        }

    @property
    def history_version(self) -> int:
        return self._history_version

    @property
    def checkpoints(self) -> tuple[CompactionCheckpoint, ...]:
        return tuple(self._checkpoints)

    def restore_checkpoints(
        self, checkpoints: tuple[CompactionCheckpoint, ...] | list[CompactionCheckpoint]
    ) -> None:
        """Restore immutable checkpoint metadata without regenerating summaries."""
        self._checkpoints = list(checkpoints)

    def clear_usage_observations(self) -> None:
        """Clear request-local calibration before restoring another session."""
        self._usage_observations.clear()
        self._latest_usage = None
        self._estimate_scale_by_profile.clear()

    @property
    def latest_usage(self) -> UsageObservation | None:
        return self._latest_usage

    @property
    def cache_epoch(self) -> int:
        return self._cache_epoch

    def estimate_request_tokens(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> int:
        """Estimate a complete request only when provider usage is unavailable."""
        message_tokens = self.get_context_tokens(messages)
        tool_tokens = len(str(tools or [])) // 3
        return max(1, message_tokens + tool_tokens)

    def observe_usage(
        self,
        *,
        actual_prompt_tokens: int,
        cached_input_tokens: int | None,
        local_request_estimate: int,
        local_history_estimate: int,
        request_boundary: str,
        model_profile: str,
    ) -> UsageObservation | None:
        """Record upstream truth and calibrate estimates per model/profile."""
        if actual_prompt_tokens <= 0:
            return None
        observation = UsageObservation.create(
            actual_prompt_tokens=actual_prompt_tokens,
            cached_input_tokens=cached_input_tokens,
            local_request_estimate=local_request_estimate,
            local_history_estimate=local_history_estimate,
            history_version=self._history_version,
            request_boundary=request_boundary,
            model_profile=model_profile,
        )
        raw_ratio = observation.actual_prompt_tokens / max(
            1, observation.local_request_estimate
        )
        ratio = min(3.0, max(0.5, raw_ratio))
        previous = self._estimate_scale_by_profile.get(model_profile)
        self._estimate_scale_by_profile[model_profile] = (
            ratio if previous is None else (previous * 0.75 + ratio * 0.25)
        )
        self._latest_usage = observation
        self._usage_observations.append(observation)
        if len(self._usage_observations) > 100:
            del self._usage_observations[:-100]
        return observation

    def predict_request_tokens(self, messages: list[dict]) -> int:
        """Predict current request size from actual usage plus calibrated growth."""
        local_history = self.get_context_tokens(messages)
        observation = self._latest_usage
        if (
            observation is not None
            and observation.history_version == self._history_version
        ):
            scale = self._estimate_scale_by_profile.get(observation.model_profile, 1.0)
            delta = local_history - observation.local_history_estimate
            return max(1, int(observation.actual_prompt_tokens + delta * scale))
        profile = observation.model_profile if observation is not None else "default"
        scale = self._estimate_scale_by_profile.get(profile, 1.0)
        fallback = (
            local_history
            + self._budget.fixed_prompt_tokens
            + self._budget.tool_schema_tokens
        )
        return max(1, int(fallback * scale))

    def maybe_compress(
        self,
        messages: list[dict],
        llm: Optional["LLM"] = None,
        *,
        history_events: tuple | list = (),
        cancellation_event=None,
    ) -> bool:
        """Commit one profitable snip or one semantic-wall rewrite epoch."""
        before_tokens = self.predict_request_tokens(messages)
        if before_tokens < self._snip_wall:
            return False

        applied_layers: list[str] = []
        candidate = [dict(message) for message in messages]
        if self._provider_compactor is not None:
            provider_result = self._provider_compactor.compact_tool_results(
                candidate, keep_recent_rounds=self._snip_keep_recent_tools
            )
            if provider_result is not None:
                candidate = provider_result.messages
                applied_layers.append("provider_tool_cache_compaction")
        snipped = self._snip_tool_outputs(candidate)
        if snipped:
            applied_layers.append("snip_tool_outputs")

        candidate_prediction = self.predict_request_tokens(candidate)
        snip_gain = max(0, before_tokens - candidate_prediction)
        semantic_due = before_tokens >= self._semantic_wall
        if not semantic_due and snip_gain < self._snip_min_gain:
            return False

        trigger = (
            "emergency"
            if before_tokens >= self._emergency_at
            else "semantic_wall"
            if semantic_due
            else "profitable_snip"
        )
        before_message_count = len(messages)
        before_snapshot = self._snapshot_messages(messages)
        self._emit_compression_started(
            before_tokens=before_tokens,
            before_message_count=before_message_count,
            before_snapshot=before_snapshot,
            trigger=trigger,
            snip_gain=snip_gain,
        )

        summarized = False
        if semantic_due:
            summarized = self._summarize_old(
                candidate,
                llm,
                keep_recent_user_turns=self._summarize_keep_recent_turns,
                checkpoint_kind="partial_prefix",
                history_events=history_events,
                cancellation_event=cancellation_event,
            )
            if summarized:
                applied_layers.append("partial_prefix")

        if not summarized and snip_gain < self._snip_min_gain:
            self._emit_compression_skipped(trigger=trigger)
            return False

        after_tokens = self.predict_request_tokens(candidate)
        if after_tokens > self._emergency_at and len(candidate) > 4:
            self._hard_collapse(
                candidate,
                llm,
                history_events=history_events,
                cancellation_event=cancellation_event,
            )
            applied_layers.append("full_recovery")
            after_tokens = self.predict_request_tokens(candidate)

        messages[:] = candidate
        source_version = self._history_version
        self._history_version += 1
        self._cache_epoch += 1
        observation = self._latest_usage
        self._checkpoints.append(
            CompactionCheckpoint.create(
                trigger=trigger,
                strategy=applied_layers,
                source_history_version=source_version,
                replacement_history=messages,
                tokens_before=before_tokens,
                tokens_after=after_tokens,
                preserved_rounds=min(
                    self._summarize_keep_recent_turns,
                    len(group_api_rounds(messages)),
                ),
                cache_epoch=self._cache_epoch,
                actual_prompt_tokens=(
                    observation.actual_prompt_tokens if observation else None
                ),
                cached_input_tokens=(
                    observation.cached_input_tokens if observation else None
                ),
                invalidated_suffix_tokens=(
                    max(0, before_tokens - (observation.cached_input_tokens or 0))
                    if observation and observation.cached_input_tokens is not None
                    else None
                ),
                reclaimed_tokens=max(0, before_tokens - after_tokens),
            )
        )
        self._latest_usage = None
        self._emit_compression_completed(
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            before_message_count=before_message_count,
            before_snapshot=before_snapshot,
            after_messages=messages,
            applied_layers=applied_layers,
            trigger=trigger,
        )
        return True

    def force_compress(
        self,
        messages: list[dict],
        strategy: str,
        llm: Optional["LLM"] = None,
        *,
        history_events: tuple | list = (),
        cancellation_event=None,
    ) -> bool:
        """Force one specific compression strategy regardless of thresholds."""
        before_tokens = self.predict_request_tokens(messages)
        before_count = len(messages)
        before_snapshot = self._snapshot_messages(messages)
        self._emit_compression_started(
            before_tokens=before_tokens,
            before_message_count=before_count,
            before_snapshot=before_snapshot,
            trigger="manual",
        )
        changed = False
        if strategy == "snip":
            changed = self._snip_tool_outputs(messages)
        elif strategy == "summarize":
            changed = self._summarize_old(
                messages,
                llm,
                keep_recent_user_turns=self._summarize_keep_recent_turns,
                checkpoint_kind="partial_prefix",
                history_events=history_events,
                cancellation_event=cancellation_event,
            )
        elif strategy == "collapse":
            if len(messages) <= 4:
                self._emit_compression_skipped(trigger="manual")
                return False
            self._hard_collapse(
                messages,
                llm,
                history_events=history_events,
                cancellation_event=cancellation_event,
            )
            changed = True
        if not changed:
            self._emit_compression_skipped(trigger="manual")
            return False

        after_tokens = self.predict_request_tokens(messages)
        source_version = self._history_version
        self._history_version += 1
        self._cache_epoch += 1
        observation = self._latest_usage
        self._checkpoints.append(
            CompactionCheckpoint.create(
                trigger="manual",
                strategy=[strategy],
                source_history_version=source_version,
                replacement_history=messages,
                tokens_before=before_tokens,
                tokens_after=after_tokens,
                preserved_rounds=min(
                    self._summarize_keep_recent_turns,
                    len(group_api_rounds(messages)),
                ),
                cache_epoch=self._cache_epoch,
                actual_prompt_tokens=(
                    observation.actual_prompt_tokens if observation else None
                ),
                cached_input_tokens=(
                    observation.cached_input_tokens if observation else None
                ),
                reclaimed_tokens=max(0, before_tokens - after_tokens),
            )
        )
        self._latest_usage = None
        self._emit_compression_completed(
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            before_message_count=before_count,
            before_snapshot=before_snapshot,
            after_messages=messages,
            applied_layers=[strategy],
            trigger="manual",
        )
        return True

    def _snip_tool_outputs(self, messages: list[dict]) -> bool:
        """Layer 1: Truncate old tool results over threshold.

        Protection is round-based rather than message-count-based: we
        walk backwards to find the N-th most recent assistant message
        that carries ``tool_calls``, then protect *all* tool messages
        from that round onwards.  A round may contain many tool calls
        and we want to keep the complete working set intact.
        """
        changed = False

        # Find the split point: count backwards by assistant messages
        # that carry tool_calls.  Each one marks the start of a round.
        assistant_rounds = 0
        cut_index = 0
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                assistant_rounds += 1
                if assistant_rounds >= self._snip_keep_recent_tools:
                    cut_index = i
                    break

        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        protected = {i for i in tool_indices if i >= cut_index}

        for i, m in enumerate(messages):
            if i in protected or m.get("role") != "tool":
                continue
            content = m.get("content", "")
            if len(content) <= self._snip_threshold_chars:
                continue
            lines = content.splitlines()
            if len(lines) <= self._snip_min_lines:
                continue
            # Keep first 3 + last 3 lines
            snipped = (
                "\n".join(lines[:3])
                + (
                    f"\n... ({len(lines)} lines, context-snipped; "
                    f"tool_call_id={m.get('tool_call_id') or 'unknown'}; "
                    "full committed output remains searchable in HistoryLedger) ...\n"
                )
                + "\n".join(lines[-3:])
            )
            m["content"] = snipped
            m.pop("_rc_token_count", None)  # invalidate stale cache
            changed = True
        return changed

    def _summarize_old(
        self,
        messages: list[dict],
        llm: Optional["LLM"],
        keep_recent_user_turns: int = 20,
        checkpoint_kind: CheckpointKind = "partial_prefix",
        history_events: tuple | list = (),
        cancellation_event=None,
    ) -> bool:
        """Layer 2: Summarize old conversation while keeping recent user turns intact."""
        split_index = self._find_recent_user_turn_boundary(
            messages, keep_recent_user_turns
        )
        split_index = self._safe_round_boundary_at_or_before(messages, split_index)
        if split_index <= 0 or split_index >= len(messages):
            return False

        old = messages[:split_index]
        tail = messages[split_index:]

        summary = self._get_summary(
            old,
            llm,
            checkpoint_kind=checkpoint_kind,
            recent_rounds_preserved=len(group_api_rounds(tail)),
            history_events=history_events,
            cancellation_event=cancellation_event,
        )

        replacement = [
            synthetic_user_message(
                "context_summary",
                summary,
                source="context_compactor",
                attributes={"kind": checkpoint_kind},
            ),
            *tail,
        ]
        messages[:] = normalize_history(replacement, reason="context compaction")
        return True

    @staticmethod
    def _find_recent_user_turn_boundary(
        messages: list[dict], keep_recent_user_turns: int
    ) -> int:
        """Return the split index that keeps the most recent N user turns and everything after them."""
        if keep_recent_user_turns <= 0:
            return len(messages)

        user_turn_starts = [
            i
            for i, msg in enumerate(messages)
            if msg.get("role") == "user" and not is_synthetic_context_message(msg)
        ]
        if len(user_turn_starts) <= keep_recent_user_turns:
            return 0
        return user_turn_starts[-keep_recent_user_turns]

    @staticmethod
    def _safe_round_boundary_at_or_before(
        messages: list[dict], split_index: int
    ) -> int:
        """Move a user-turn split backward so tool calls and results stay adjacent."""
        offset = 0
        for round_ in group_api_rounds(messages):
            next_offset = offset + len(round_.messages)
            if split_index < next_offset:
                return offset
            offset = next_offset
        return min(split_index, len(messages))

    def _hard_collapse(
        self,
        messages: list[dict],
        llm: Optional["LLM"],
        *,
        history_events: tuple | list = (),
        cancellation_event=None,
    ) -> None:
        """Layer 3: Emergency compression."""
        split_index = recent_round_start(messages, 2)
        tail = messages[split_index:]
        summary = self._get_summary(
            messages[:split_index],
            llm,
            checkpoint_kind="full_recovery",
            recent_rounds_preserved=len(group_api_rounds(tail)),
            history_events=history_events,
            cancellation_event=cancellation_event,
        )

        messages.clear()
        messages.extend(
            normalize_history(
                [
                    synthetic_user_message(
                        "context_summary",
                        summary,
                        source="context_compactor",
                        attributes={"kind": "full_recovery"},
                    ),
                    *tail,
                ],
                reason="hard context compaction",
            )
        )

    def _get_summary(
        self,
        messages: list[dict],
        llm: Optional["LLM"],
        *,
        checkpoint_kind: CheckpointKind,
        recent_rounds_preserved: int,
        history_events: tuple | list = (),
        cancellation_event=None,
    ) -> str:
        """Generate summary via LLM or fallback to extraction."""
        try:
            summary = generate_summary(
                messages,
                llm,
                checkpoint_kind=checkpoint_kind,
                summarized_history_version=self._history_version,
                recent_rounds_preserved=recent_rounds_preserved,
                history_events=history_events,
                cancellation_event=cancellation_event,
            )
        except Exception:
            if cancellation_event is not None and cancellation_event.is_set():
                raise
            self._consecutive_summary_failures += 1
            return self._extract_key_info(messages)
        self._consecutive_summary_failures = 0
        return summary

    def _emit_compression_started(
        self,
        *,
        before_tokens: int,
        before_message_count: int,
        before_snapshot: list[dict[str, Any]],
        trigger: str,
        snip_gain: int | None = None,
    ) -> None:
        """Tell the UI before any potentially slow compaction work begins."""
        if not self._ui_bus:
            return

        capacity = before_tokens / max(1, self.request_input_limit)
        operation = (
            "Context compression" if trigger == "manual" else "Context auto-compression"
        )
        gain_text = ""
        if snip_gain is not None:
            gain_ratio = snip_gain / max(1, self.request_input_limit)
            gain_text = f"; deterministic snip gain: {snip_gain} ({gain_ratio:.1%})"
        self._ui_bus.info(
            (
                f"{operation} started: "
                f"{before_tokens} tokens / {before_message_count} messages "
                f"({capacity:.1%} capacity; reason: {trigger}{gain_text})."
            ),
            kind=self._context_event_kind(),
            phase="before",
            trigger=trigger,
            trigger_tokens=before_tokens,
            trigger_message_count=before_message_count,
            capacity_ratio=capacity,
            max_tokens=self.max_tokens,
            thresholds={
                "snip_wall": self._snip_wall,
                "semantic_wall": self._semantic_wall,
                "snip_min_gain": self._snip_min_gain,
                "target": self._rewrite_target,
                "emergency_at": self._emergency_at,
            },
            snip_gain=snip_gain,
            context_snapshot=before_snapshot,
        )

    def _emit_compression_skipped(self, *, trigger: str) -> None:
        """Close a started UI operation when compaction found no safe rewrite."""
        if not self._ui_bus:
            return
        self._ui_bus.info(
            "Context compression made no changes.",
            kind=self._context_event_kind(),
            phase="skipped",
            trigger=trigger,
        )

    def _emit_compression_completed(
        self,
        *,
        before_tokens: int,
        after_tokens: int,
        before_message_count: int,
        before_snapshot: list[dict[str, Any]],
        after_messages: list[dict],
        applied_layers: list[str],
        trigger: str,
    ) -> None:
        """Push the completion event using the planner's calibrated estimate."""
        if not self._ui_bus:
            return

        after_message_count = len(after_messages)
        after_snapshot = self._snapshot_messages(after_messages)
        strategy = self._describe_strategy(applied_layers)
        delta_tokens = after_tokens - before_tokens
        delta_messages = after_message_count - before_message_count
        operation = (
            "Context compression" if trigger == "manual" else "Context auto-compression"
        )

        self._ui_bus.success(
            (
                f"{operation} completed: "
                f"{before_tokens} → ~{after_tokens} tokens, "
                f"{before_message_count} → {after_message_count} messages."
            ),
            kind=self._context_event_kind(),
            phase="after",
            trigger=trigger,
            strategy=strategy,
            applied_layers=applied_layers,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            token_delta=delta_tokens,
            before_message_count=before_message_count,
            after_message_count=after_message_count,
            message_delta=delta_messages,
            before_context=before_snapshot,
            after_context=after_snapshot,
        )

    @staticmethod
    def _snapshot_messages(
        messages: list[dict], max_items: int = 12, max_chars: int = 240
    ) -> list[dict[str, Any]]:
        """Create a compact, UI-friendly snapshot of current context."""
        if len(messages) <= max_items:
            selected = list(enumerate(messages))
        else:
            head_count = max_items // 2
            tail_count = max_items - head_count
            selected = list(enumerate(messages[:head_count]))
            selected.append(
                (
                    -1,
                    {
                        "role": "meta",
                        "content": f"... {len(messages) - max_items} messages omitted ...",
                    },
                )
            )
            selected.extend(
                (len(messages) - tail_count + i, msg)
                for i, msg in enumerate(messages[-tail_count:])
            )

        snapshot: list[dict[str, Any]] = []
        for index, msg in selected:
            role = msg.get("role", "?")
            content = (msg.get("content", "") or "").replace("\r", "")
            if len(content) > max_chars:
                content = content[: max_chars - 3] + "..."
            item: dict[str, Any] = {
                "index": index,
                "role": role,
                "content": content,
            }
            if msg.get("tool_call_id"):
                item["tool_call_id"] = msg["tool_call_id"]
            if msg.get("tool_calls"):
                item["tool_calls"] = msg["tool_calls"]
            snapshot.append(item)
        return snapshot

    def _describe_strategy(self, applied_layers: list[str]) -> dict[str, Any]:
        """Describe configured compression policy and actual applied layers."""
        return {
            "policy": [
                {
                    "layer": "profitable_snip",
                    "threshold": self._snip_wall,
                    "description": "From 60% of request capacity, commit deterministic tool-output snipping only when it reclaims at least 20% of total request capacity.",
                },
                {
                    "layer": "semantic_checkpoint",
                    "threshold": self._semantic_wall,
                    "description": f"At 75% of request capacity, batch deterministic snip and semantic summary in one cache epoch, preserving {self._summarize_keep_recent_turns} recent API rounds and targeting about 40%.",
                },
                {
                    "layer": "hard_collapse",
                    "threshold": self._emergency_at,
                    "description": "At 90% of request capacity, perform last-resort recovery while preserving complete recent API rounds.",
                },
            ],
            "applied_layers": applied_layers,
        }

    @staticmethod
    def _context_event_kind():
        from reuleauxcoder.interfaces.events import UIEventKind

        return UIEventKind.CONTEXT

    @staticmethod
    def _flatten(messages: list[dict]) -> str:
        """Flatten messages to string."""
        parts = []
        for m in messages:
            role = m.get("role", "?")
            text = m.get("content", "") or ""
            if text:
                parts.append(f"[{role}] {text[:400]}")
        return "\n".join(parts)

    @staticmethod
    def _extract_key_info(messages: list[dict]) -> str:
        """Fallback: extract file paths, errors, and decisions."""
        import re

        files_seen = set()
        errors = []

        for m in messages:
            text = m.get("content", "") or ""
            # Extract file paths
            for match in re.finditer(r"[\w./\-]+\.\w{1,5}", text):
                files_seen.add(match.group())
            # Extract error lines
            for line in text.splitlines():
                if "error" in line.lower() or "Error" in line:
                    errors.append(line.strip()[:150])

        parts = []
        if files_seen:
            parts.append(f"Files touched: {', '.join(sorted(files_seen)[:20])}")
        if errors:
            parts.append(f"Errors seen: {'; '.join(errors[:5])}")
        return "\n".join(parts) or "(no extractable context)"
