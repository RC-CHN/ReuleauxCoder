"""Context manager - manages conversation context and compression."""

from __future__ import annotations
from typing import TYPE_CHECKING, Optional, Any

if TYPE_CHECKING:
    from reuleauxcoder.services.llm.client import LLM
    from reuleauxcoder.interfaces.events import UIEventBus

# Tiktoken encoder cache
_tiktoken_encoder = None
MESSAGE_TOKEN_KEY = "_rc_token_count"


def _get_tiktoken_encoder():
    """Get or create tiktoken encoder (o200k_base for modern models)."""
    global _tiktoken_encoder
    if _tiktoken_encoder is None:
        try:
            import tiktoken

            _tiktoken_encoder = tiktoken.get_encoding("o200k_base")
        except Exception:
            _tiktoken_encoder = None
    return _tiktoken_encoder


def _estimate_message_tokens_chars(message: dict) -> int:
    """Estimate token count for a single message using chars/3 (fallback)."""
    total = 0
    if message.get("content"):
        total += len(str(message["content"])) // 3
    if message.get("tool_calls"):
        total += len(str(message["tool_calls"])) // 3
    return total


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
        total += estimate_message_tokens(message, refresh=refresh, token_fudge_factor=token_fudge_factor)
    return total


def estimate_tokens_tiktoken(messages: list[dict], token_fudge_factor: float = 1.1) -> int:
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

SNIP_DEBOUNCE_TOKENS = 2_000
SUMMARIZE_DEBOUNCE_TOKENS = 4_000


class ContextManager:
    """Manages conversation context with multi-layer compression."""

    def __init__(
        self,
        max_tokens: int = 128_000,
        ui_bus: "UIEventBus | None" = None,
        snip_keep_recent_tools: int = 5,
        snip_threshold_chars: int = 1500,
        snip_min_lines: int = 6,
        summarize_keep_recent_turns: int = 5,
        token_fudge_factor: float = 1.1,
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
        # Layer thresholds (fraction of max_tokens)
        self._snip_at = int(max_tokens * 0.50)  # 50% -> snip tool outputs
        self._summarize_at = int(max_tokens * 0.70)  # 70% -> LLM summarize
        self._collapse_at = int(max_tokens * 0.90)  # 90% -> hard collapse
        # State tracking
        self._last_compact_tokens = 0
        self._last_compact_strategy: str | None = None
        # Wall-hit counters for progressive compression
        self._snip_exhausted = False
        self._summarize_exhausted = False
        self._snip_hit_count = 0
        self._summarize_hit_count = 0
        self._max_hits = 3

    def get_context_tokens(self, messages: list[dict]) -> int:
        """Get current locally-estimated context token count."""
        return estimate_tokens(messages, token_fudge_factor=self._token_fudge_factor)

    def _reset_compression_state(self) -> None:
        """Reset all compression wall-hit counters and exhausted flags."""
        self._snip_exhausted = False
        self._summarize_exhausted = False
        self._snip_hit_count = 0
        self._summarize_hit_count = 0

    def reconfigure(self, max_tokens: int) -> None:
        """Update context budget and recompute layer thresholds."""
        self.max_tokens = max_tokens
        self._snip_at = int(max_tokens * 0.50)
        self._summarize_at = int(max_tokens * 0.70)
        self._collapse_at = int(max_tokens * 0.90)
        self._reset_compression_state()

    def maybe_compress(
        self,
        messages: list[dict],
        llm: Optional["LLM"] = None,
    ) -> bool:
        """Apply compression layers as needed.

        Returns True if any compression happened.
        """
        before_tokens = self.get_context_tokens(messages)
        before_message_count = len(messages)
        before_snapshot = self._snapshot_messages(messages)
        compressed = False
        applied_layers: list[str] = []

        current = before_tokens

        # Layer 3: Hard collapse - unconditional fallback
        if current > self._collapse_at and len(messages) > 4:
            self._hard_collapse(messages, llm)
            compressed = True
            applied_layers.append("hard_collapse")
            self._last_compact_strategy = "collapse"
            self._reset_compression_state()
            current = self.get_context_tokens(messages)

        # Layer 2: Summarize old conversation
        elif current > self._summarize_at:
            # If snip is exhausted or summarize is near exhausted, go straight to summarize
            if self._snip_exhausted or self._summarize_hit_count >= self._max_hits - 1:
                changed = self._summarize_old(
                    messages,
                    llm,
                    keep_recent_user_turns=self._summarize_keep_recent_turns,
                )
                if changed:
                    compressed = True
                    applied_layers.append("summarize_old")
                    self._last_compact_strategy = "summarize"
                    current = self.get_context_tokens(messages)

                    if current <= self._summarize_at:
                        # Successfully reduced below threshold
                        self._reset_compression_state()
                    else:
                        # Summarize didn't help enough
                        self._summarize_hit_count += 1
                        if self._summarize_hit_count >= self._max_hits:
                            self._summarize_exhausted = True
                else:
                    # Summarize couldn't run (no LLM or not enough messages) - mark exhausted
                    self._summarize_exhausted = True
            else:
                # Try snip first (layer 1)
                if current > self._snip_at and not self._snip_exhausted:
                    snip_changed = self._snip_tool_outputs(messages)
                    if snip_changed:
                        compressed = True
                        applied_layers.append("snip_tool_outputs")
                        self._last_compact_strategy = "snip"
                        current = self.get_context_tokens(messages)

                        if current <= self._snip_at:
                            # Successfully reduced below threshold
                            self._reset_compression_state()
                        else:
                            # Snip didn't help enough
                            self._snip_hit_count += 1
                            if self._snip_hit_count >= self._max_hits:
                                self._snip_exhausted = True
                    else:
                        # Snip had nothing to compress
                        self._snip_exhausted = True

                # After snip attempt, check if we still need summarize
                if current > self._summarize_at and not self._summarize_exhausted:
                    summarize_changed = self._summarize_old(
                        messages,
                        llm,
                        keep_recent_user_turns=self._summarize_keep_recent_turns,
                    )
                    if summarize_changed:
                        compressed = True
                        applied_layers.append("summarize_old")
                        self._last_compact_strategy = "summarize"
                        current = self.get_context_tokens(messages)

                        if current <= self._summarize_at:
                            self._reset_compression_state()
                        else:
                            self._summarize_hit_count += 1
                            if self._summarize_hit_count >= self._max_hits:
                                self._summarize_exhausted = True
                    else:
                        # Summarize couldn't run
                        self._summarize_exhausted = True

        # Layer 1: Snip tool outputs (when below summarize threshold)
        elif current > self._snip_at and not self._snip_exhausted:
            changed = self._snip_tool_outputs(messages)
            if changed:
                compressed = True
                applied_layers.append("snip_tool_outputs")
                self._last_compact_strategy = "snip"
                current = self.get_context_tokens(messages)

                if current <= self._snip_at:
                    self._reset_compression_state()
                else:
                    # Snip ran but didn't reduce enough
                    self._snip_hit_count += 1
                    if self._snip_hit_count >= self._max_hits:
                        self._snip_exhausted = True
            else:
                # Snip had nothing to compress - mark as exhausted immediately
                self._snip_exhausted = True

        # Context is healthy - reset state
        if current <= self._snip_at:
            self._reset_compression_state()

        if compressed:
            self._last_compact_tokens = current
            self._emit_compression_events(
                before_tokens=before_tokens,
                before_message_count=before_message_count,
                before_snapshot=before_snapshot,
                after_messages=messages,
                applied_layers=applied_layers,
            )

        return compressed

    def force_compress(
        self,
        messages: list[dict],
        strategy: str,
        llm: Optional["LLM"] = None,
    ) -> bool:
        """Force one specific compression strategy regardless of thresholds."""
        if strategy == "snip":
            changed = self._snip_tool_outputs(messages)
            if changed:
                self._last_compact_tokens = estimate_tokens(messages, token_fudge_factor=self._token_fudge_factor)
                self._last_compact_strategy = "snip"
            return changed
        if strategy == "summarize":
            changed = self._summarize_old(
                messages, llm, keep_recent_user_turns=self._summarize_keep_recent_turns
            )
            if changed:
                self._last_compact_tokens = estimate_tokens(messages, token_fudge_factor=self._token_fudge_factor)
                self._last_compact_strategy = "summarize"
            return changed
        if strategy == "collapse":
            if len(messages) <= 4:
                return False
            self._hard_collapse(messages, llm)
            self._last_compact_tokens = estimate_tokens(messages, token_fudge_factor=self._token_fudge_factor)
            self._last_compact_strategy = "collapse"
            return True
        return False

    def _snip_tool_outputs(self, messages: list[dict]) -> bool:
        """Layer 1: Truncate older tool results over threshold, keeping recent tool outputs intact."""
        changed = False
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        protected = set(tool_indices[-self._snip_keep_recent_tools :])

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
                + f"\n... ({len(lines)} lines, snipped to save context) ...\n"
                + "\n".join(lines[-3:])
            )
            m["content"] = snipped
            changed = True
        return changed

    def _summarize_old(
        self,
        messages: list[dict],
        llm: Optional["LLM"],
        keep_recent_user_turns: int = 20,
    ) -> bool:
        """Layer 2: Summarize old conversation while keeping recent user turns intact."""
        split_index = self._find_recent_user_turn_boundary(
            messages, keep_recent_user_turns
        )
        if split_index <= 0 or split_index >= len(messages):
            return False

        old = messages[:split_index]
        tail = messages[split_index:]

        summary = self._get_summary(old, llm)

        messages.clear()
        messages.append(
            {
                "role": "user",
                "content": f"[Context compressed - conversation summary]\n{summary}",
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "Got it, I have the context from our earlier conversation.",
            }
        )
        messages.extend(tail)
        return True

    @staticmethod
    def _find_recent_user_turn_boundary(
        messages: list[dict], keep_recent_user_turns: int
    ) -> int:
        """Return the split index that keeps the most recent N user turns and everything after them."""
        if keep_recent_user_turns <= 0:
            return len(messages)

        user_turn_starts = [
            i for i, msg in enumerate(messages) if msg.get("role") == "user"
        ]
        if len(user_turn_starts) <= keep_recent_user_turns:
            return 0
        return user_turn_starts[-keep_recent_user_turns]

    def _hard_collapse(
        self,
        messages: list[dict],
        llm: Optional["LLM"],
    ) -> None:
        """Layer 3: Emergency compression."""
        tail = messages[-4:] if len(messages) > 4 else messages[-2:]
        summary = self._get_summary(messages[: -len(tail)], llm)

        messages.clear()
        messages.append(
            {
                "role": "user",
                "content": f"[Hard context reset]\n{summary}",
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "Context restored. Continuing from where we left off.",
            }
        )
        messages.extend(tail)

    def _get_summary(
        self,
        messages: list[dict],
        llm: Optional["LLM"],
    ) -> str:
        """Generate summary via LLM or fallback to extraction."""
        flat = self._flatten(messages)

        if llm:
            try:
                resp = llm.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": SUMMARY_SYSTEM_PROMPT,
                        },
                        {"role": "user", "content": flat[:20000]},
                    ],
                )
                return resp.content
            except Exception:
                pass

        # Fallback: extract key lines
        return self._extract_key_info(messages)

    def _emit_compression_events(
        self,
        *,
        before_tokens: int,
        before_message_count: int,
        before_snapshot: list[dict[str, Any]],
        after_messages: list[dict],
        applied_layers: list[str],
    ) -> None:
        """Push UI events describing context compression lifecycle."""
        if not self._ui_bus:
            return

        after_tokens = estimate_tokens(after_messages)
        after_message_count = len(after_messages)
        after_snapshot = self._snapshot_messages(after_messages)
        strategy = self._describe_strategy(applied_layers)
        delta_tokens = after_tokens - before_tokens
        delta_messages = after_message_count - before_message_count

        self._ui_bus.info(
            f"Context auto-compression triggered at {before_tokens} tokens / {before_message_count} messages.",
            kind=self._context_event_kind(),
            phase="before",
            trigger_tokens=before_tokens,
            trigger_message_count=before_message_count,
            max_tokens=self.max_tokens,
            thresholds={
                "snip_at": self._snip_at,
                "summarize_at": self._summarize_at,
                "collapse_at": self._collapse_at,
            },
            strategy=strategy,
            applied_layers=applied_layers,
            context_snapshot=before_snapshot,
        )
        self._ui_bus.success(
            (
                "Context auto-compression completed: "
                f"{before_tokens} → {after_tokens} tokens, "
                f"{before_message_count} → {after_message_count} messages."
            ),
            kind=self._context_event_kind(),
            phase="after",
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
                    "layer": "snip_tool_outputs",
                    "threshold": self._snip_at,
                    "description": "When context usage exceeds 50% of the budget, truncate older verbose tool outputs.",
                },
                {
                    "layer": "summarize_old",
                    "threshold": self._summarize_at,
                    "description": "When context usage exceeds 70% of the budget and enough history exists, summarize older conversation and keep the most recent 20 user turns.",
                },
                {
                    "layer": "hard_collapse",
                    "threshold": self._collapse_at,
                    "description": "When context usage exceeds 90% of the budget, perform a hard collapse and keep only the summary plus the most recent tail messages.",
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
