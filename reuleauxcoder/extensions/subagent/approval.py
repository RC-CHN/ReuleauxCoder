"""Approval delegation helpers for sub-agents.

Provides a ``ParentLLMJudge`` that the parent-agent LLM can use to
pre-approve or deny sub-agent tool calls, and
``build_subagent_approval_provider`` which returns a
``SharedApprovalProvider`` with the judge wired in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading

from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
    ApprovalHandler,
    PendingApproval,
    SharedApprovalProvider,
)

_PARENT_LLM_JUDGE_TIMEOUT_SECONDS = 15


# ── ParentLLMJudge (pre-approval judge for sub-agents) ─────────────────


@dataclass(slots=True)
class ParentLLMJudge:
    """Use parent-agent LLM as a pre-approval judge for sub-agent tools.

    Implements the ``ApprovalJudge`` protocol: ``__call__`` returns an
    ``ApprovalDecision`` (allow / deny) or ``None`` (escalate to human).
    """

    parent_agent: object
    subagent_mode: str
    subagent_task: str

    def __call__(self, request: ApprovalRequest) -> ApprovalDecision | None:
        llm = getattr(self.parent_agent, "llm", None)
        if llm is None:
            return None  # escalate

        # Build enriched request for the judge prompt
        enriched = ApprovalRequest(
            tool_name=request.tool_name,
            tool_args=dict(request.tool_args),
            tool_source=request.tool_source,
            reason=request.reason,
            metadata={
                **request.metadata,
                "is_subagent": True,
                "subagent_mode": self.subagent_mode,
                "subagent_task": self.subagent_task,
            },
        )

        decision, reason = _judge_via_llm(llm, enriched)
        if decision == "allow":
            return ApprovalDecision.allow_once(reason or "approved by parent llm")
        if decision == "deny":
            return ApprovalDecision.deny_once(reason or "denied by parent llm")
        return None  # escalate


# ── Sub-agent approval provider factory ────────────────────────────────


def build_subagent_approval_provider(
    parent_agent,
    subagent_mode: str,
    subagent_task: str,
) -> ApprovalProvider | None:
    """Create a ``SharedApprovalProvider`` for a sub-agent.

    The returned provider:
    1. Lets the parent LLM judge auto-approve/deny safe calls.
    2. Falls back to the parent's human handler for escalated requests.

    An ``RLock`` on the parent agent serialises escalation so that
    multiple sub-agents don't compete for terminal / dialog access.
    """
    parent_provider: SharedApprovalProvider | None = getattr(
        parent_agent, "approval_provider", None
    )
    if parent_provider is None:
        return None

    lock = _get_parent_approval_lock(parent_agent)
    parent_handler = parent_provider.handler

    def locked_handler(pending: PendingApproval) -> None:
        # Enrich the request with sub-agent attribution so the human
        # handler (CLI / TUI) can display the sub-agent source.
        pending.request.metadata.update({
            "is_subagent": True,
            "subagent_mode": subagent_mode,
            "subagent_task": subagent_task,
        })
        pending.request.reason = (
            f"{pending.request.reason or 'sub-agent approval request'}; "
            "parent_llm=escalate"
        )
        with lock:
            parent_handler(pending)

    return SharedApprovalProvider(
        handler=locked_handler,
        judges=[ParentLLMJudge(parent_agent, subagent_mode, subagent_task)],
    )


# ── Internal helpers ───────────────────────────────────────────────────


def _judge_via_llm(llm, request: ApprovalRequest) -> tuple[str, str]:
    """Call parent LLM in a daemon thread with a 15 s timeout.

    Returns ``(decision, reason)`` where *decision* is one of
    ``allow``, ``deny``, or ``escalate``.
    """
    prompt = _build_parent_judge_prompt(request)
    messages = [
        {
            "role": "system",
            "content": (
                "You are an approval judge for sub-agent tool calls. "
                "Return strict JSON only: "
                '{"decision":"allow|deny|escalate","reason":"short reason"}. '
                "Choose escalate when uncertain."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    holder: dict[str, tuple[str, str]] = {}

    def _run() -> None:
        try:
            resp = llm.chat(messages=messages, tools=None)
            content = (resp.content or "").strip()
            holder["result"] = _parse_judge_response(content)
        except Exception as e:
            holder["result"] = ("escalate", f"parent llm judge failed: {e}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=_PARENT_LLM_JUDGE_TIMEOUT_SECONDS)
    if thread.is_alive():
        return (
            "escalate",
            f"parent llm judge timed out after {_PARENT_LLM_JUDGE_TIMEOUT_SECONDS}s",
        )
    return holder.get("result", ("escalate", "parent llm judge returned no result"))


def _get_parent_approval_lock(parent_agent):
    """Return (or create) the per-parent-agent RLock for sub-agent escalation."""
    lock = getattr(parent_agent, "_subagent_approval_lock", None)
    if lock is not None and hasattr(lock, "acquire") and hasattr(lock, "release"):
        return lock
    lock = threading.RLock()
    setattr(parent_agent, "_subagent_approval_lock", lock)
    return lock


def _build_parent_judge_prompt(request: ApprovalRequest) -> str:
    return (
        "Decide whether this sub-agent tool call should be approved.\n"
        f"subagent_mode: {request.metadata.get('subagent_mode')}\n"
        f"subagent_task: {request.metadata.get('subagent_task')}\n"
        f"tool_name: {request.tool_name}\n"
        f"tool_source: {request.tool_source}\n"
        f"reason: {request.reason or '-'}\n"
        f"tool_args: {json.dumps(request.tool_args, ensure_ascii=False)}\n"
        "Policy: prioritize safety. If unsure, choose escalate."
    )


def _parse_judge_response(text: str) -> tuple[str, str]:
    if not text:
        return "escalate", "empty llm judge response"

    parsed: dict | None = None
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except Exception:
                parsed = None

    if not isinstance(parsed, dict):
        return "escalate", "invalid llm judge json"

    decision = str(parsed.get("decision", "escalate")).strip().lower()
    reason = str(parsed.get("reason", "")).strip()
    if decision not in {"allow", "deny", "escalate"}:
        return "escalate", "unknown llm judge decision"
    return decision, reason
