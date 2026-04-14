"""Approval delegation helpers for sub-agents."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading

from reuleauxcoder.domain.approval import ApprovalDecision, ApprovalProvider, ApprovalRequest

_PARENT_LLM_JUDGE_TIMEOUT_SECONDS = 15


@dataclass(slots=True)
class ParentLLMApprovalJudge:
    """Use parent-agent LLM as secondary judge for sub-agent approvals."""

    parent_agent: object

    def decide(self, request: ApprovalRequest) -> tuple[str, str]:
        """Return (decision, reason), where decision in allow|deny|escalate."""
        llm = getattr(self.parent_agent, "llm", None)
        if llm is None:
            return "escalate", "parent agent has no llm"

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


@dataclass(slots=True)
class DelegatingSubagentApprovalProvider(ApprovalProvider):
    """Route sub-agent approvals: parent LLM first, human fallback."""

    parent_provider: ApprovalProvider
    parent_agent: object
    subagent_mode: str
    subagent_task: str
    judge: ParentLLMApprovalJudge = field(init=False)

    def __post_init__(self) -> None:
        self.judge = ParentLLMApprovalJudge(self.parent_agent)

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        enriched = ApprovalRequest(
            tool_name=request.tool_name,
            tool_args=dict(request.tool_args),
            tool_source=request.tool_source,
            effect_class=request.effect_class,
            reason=request.reason,
            profile=request.profile,
            metadata={
                **request.metadata,
                "is_subagent": True,
                "subagent_mode": self.subagent_mode,
                "subagent_task": self.subagent_task,
            },
        )

        approval_lock = _get_parent_approval_lock(self.parent_agent)
        with approval_lock:
            decision, reason = self.judge.decide(enriched)
            if decision == "allow":
                return ApprovalDecision.allow_once(reason or "approved by parent llm")
            if decision == "deny":
                return ApprovalDecision.deny_once(reason or "denied by parent llm")

            # Uncertain / failed judge -> ask human via parent provider.
            enriched.reason = (
                f"{enriched.reason or 'sub-agent approval request'}; "
                f"parent_llm=escalate ({reason or 'unsure'})"
            )
            return self.parent_provider.request_approval(enriched)


def build_subagent_approval_provider(
    parent_agent,
    subagent_mode: str,
    subagent_task: str,
) -> ApprovalProvider | None:
    parent_provider = getattr(parent_agent, "approval_provider", None)
    if parent_provider is None:
        return None
    return DelegatingSubagentApprovalProvider(
        parent_provider=parent_provider,
        parent_agent=parent_agent,
        subagent_mode=subagent_mode,
        subagent_task=subagent_task,
    )


def _get_parent_approval_lock(parent_agent):
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
