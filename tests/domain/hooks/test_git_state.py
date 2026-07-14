from __future__ import annotations

from reuleauxcoder.domain.hooks.builtin.git_state import (
    GitStateInjectorHook,
    render_git_snapshot,
)
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint


def _tail() -> dict:
    return {
        "role": "user",
        "content": (
            '<execution_state plan_revision="0">\n'
            '<execution_data trust="untrusted_data">{}</execution_data>\n'
            "<runtime_instruction>Continue.</runtime_instruction>\n"
            "</execution_state>"
        ),
    }


def _snapshot() -> dict:
    return {
        "repository_root": "/repo",
        "available": True,
        "branch": "main",
        "head": "abc123 initial",
        "changes": {
            "staged": {
                "count": 5,
                "items": ["modified a.py", "… and 4 more (mostly src/**)"],
            },
            "unstaged": {"count": 0, "items": []},
            "untracked": {"count": 0, "items": []},
        },
        "status_output_truncated": False,
        "head_change": {
            "kind": "new_commits",
            "from": "old",
            "to": "abc123",
            "count": 1,
            "commits": ["abc123 initial"],
        },
    }


class _Monitor:
    def __init__(self, snapshot):
        self.value = snapshot
        self.turn_ids = []

    def snapshot(self, *, turn_id=None):
        self.turn_ids.append(turn_id)
        return self.value

    @staticmethod
    def compact(snapshot):
        return snapshot


def test_git_hook_injects_readable_untrusted_region_before_instruction() -> None:
    monitor = _Monitor(_snapshot())
    hook = GitStateInjectorHook(git_monitor=monitor)
    context = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[_tail()],
        turn_id="turn-7",
    )

    result = hook.run(context)
    content = result.messages[-1]["content"]

    assert monitor.turn_ids == ["turn-7"]
    assert content.index("<git_state") < content.index("<runtime_instruction>")
    assert "branch: main" in content
    assert "staged (5):" in content
    assert "… and 4 more (mostly src/**)" in content
    assert "head change: new_commits old -&gt; abc123, 1 commit(s)" in content


def test_git_hook_does_not_observe_without_a_valid_runtime_tail() -> None:
    monitor = _Monitor(_snapshot())
    hook = GitStateInjectorHook(git_monitor=monitor)
    context = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[{"role": "user", "content": "hello"}],
    )

    assert hook.run(context) is context
    assert monitor.turn_ids == []


def test_git_hook_failure_never_blocks_the_request() -> None:
    class _BrokenMonitor:
        def snapshot(self, **_kwargs):
            raise RuntimeError("git unavailable")

    context = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[_tail()],
    )

    assert GitStateInjectorHook(git_monitor=_BrokenMonitor()).run(context) is context
    assert "<git_state" not in context.messages[-1]["content"]


def test_git_render_is_lightly_structured_and_human_readable() -> None:
    rendered = render_git_snapshot(_snapshot())

    assert "repository: /repo" in rendered
    assert "staged (5):\n  - modified a.py" in rendered
    assert '"changes"' not in rendered


def test_git_render_explicitly_reports_an_uninitialized_workspace() -> None:
    rendered = render_git_snapshot(
        {
            "repository_root": "/not-a-repository",
            "available": False,
            "reason": "not_initialized",
            "truncated": False,
        }
    )

    assert rendered == "git: not initialized in current workspace"


def test_git_render_explicitly_reports_a_missing_executable() -> None:
    rendered = render_git_snapshot(
        {
            "repository_root": "/workspace",
            "available": False,
            "reason": "git_not_installed",
            "truncated": False,
        }
    )

    assert rendered == "git: unavailable — executable not found on this system"
