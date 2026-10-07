"""Real detached Python processes and a local OpenAI-compatible test endpoint."""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from reuleauxcoder.infrastructure.persistence.job_store import JobStore, lease


class ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        self.server.arrived.set()
        self.server.release.wait(30)
        tool_result = any(
            m["role"] == "tool" and "complete" in str(m.get("content", ""))
            for m in request["messages"]
        )
        delta = (
            {"content": "任务完成，交接已保存。"}
            if tool_result
            else {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "complete_goal",
                        "type": "function",
                        "function": {
                            "name": "update_goal",
                            "arguments": '{"status":"complete"}',
                        },
                    }
                ]
            }
        )
        if getattr(self.server, "write_file", False) and not any(
            m["role"] == "tool" and m.get("tool_call_id") == "write_result"
            for m in request["messages"]
        ):
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "write_result",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"file_path": "result.txt", "content": "审批后写入"}
                            ),
                        },
                    }
                ]
            }

        def chunk(delta, finish=None):
            return {
                "id": "job-test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "job-test",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        body = (
            "".join(
                "data: " + json.dumps(v) + "\n\n"
                for v in [
                    chunk(delta),
                    chunk({}, "stop" if tool_result else "tool_calls"),
                ]
            )
            + "data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        try:
            self.wfile.write(body.encode())
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_):
        pass


@pytest.fixture
def cli(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    server.requests = []
    server.arrived = threading.Event()
    server.release = threading.Event()
    server.release.set()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    workspace = tmp_path / "工作区"
    workspace.mkdir()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "active_main": "job-test",
                    "active_sub": "job-test",
                    "profiles": {
                        "job-test": {
                            "model": "job-test",
                            "api_key": "test",
                            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                            "provider": "openai-compatible",
                            "request_mode": "chat-completions",
                        }
                    },
                },
                "lsp": {"enabled": False},
                "mcp": {"servers": {}},
                "remote_exec": {"enabled": False},
                "approval": {"default_mode": "require_approval"},
            }
        ),
        encoding="utf-8",
    )
    store = JobStore(tmp_path / "jobs")
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env["PYTHONUTF8"] = "1"

    def run(*args):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "reuleauxcoder",
                "job",
                "--store",
                str(store.root),
                *args,
            ],
            capture_output=True,
            check=False,
            env=env,
            timeout=30,
        )
        return (
            result.returncode,
            json.loads(result.stdout.decode("utf-8")) if result.stdout else None,
            result.stderr.decode("utf-8"),
        )

    def start(key="launch"):
        return run(
            "start",
            "--id",
            key,
            "--workspace",
            str(workspace),
            "--config",
            str(config),
            "--task",
            "在当前工作区完成任务，并保存成果和交接记录。",
            "--detach",
        )

    try:
        yield run, start, store, server, workspace
    finally:
        server.release.set()
        for path in store.root.iterdir():
            if path.is_dir() and (path / "state.json").exists():
                state = store.status(path.name)
                if state["alive"]:
                    try:
                        store.control(path.name, "cancel")
                    except ValueError:
                        pass
        server.shutdown()
        server.server_close()


def wait_status(store, key, expected):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = store.status(key)
        if state["status"] in expected and not state["alive"]:
            return state
        time.sleep(0.05)
    log = store.path(key) / "worker.log"
    pytest.fail(f"{state}\n{log.read_text(encoding='utf-8') if log.exists() else ''}")


def test_detached_cli_completes_and_duplicate_launch_is_idempotent(cli):
    run, start, store, server, _ = cli
    code, result, error = start()
    assert code == 0, (result, error)
    assert result["launch_state"] == "admitted", result
    final = wait_status(store, "launch", {"completed", "failed", "blocked"})
    assert final["status"] == "completed", run("result", "launch")
    assert final["response"] == "任务完成，交接已保存。"
    count = len(server.requests)
    assert start()[0] == 0
    assert len(server.requests) == count
    assert run("resume", "launch", "--detach")[0] == 0
    assert store.status("launch")["session_id"] == final["session_id"]


def test_detached_workspace_conflict_reports_failure(cli):
    _, start, store, server, workspace = cli
    (workspace / ".rcoder").mkdir()
    with lease(workspace / ".rcoder/unattended.lock"):
        code, result, _ = start()
    assert code != 0 and result["launch_state"] == "failed"
    assert store.status("launch")["status"] == "queued"
    assert not server.requests


def test_killed_worker_and_concurrent_resume_keep_one_session(cli):
    run, start, store, server, _ = cli
    server.release.clear()
    assert start()[0] == 0
    assert server.arrived.wait(10)
    first = store.status("launch")
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(first["pid"])],
            capture_output=True,
            check=True,
        )
    else:
        import signal

        os.kill(first["pid"], signal.SIGKILL)
    wait_status(store, "launch", {"interrupted"})
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda _: run("resume", "launch", "--detach"), range(2))
        )
    assert any(code == 0 for code, _, _ in results), results
    active = store.status("launch")
    assert active["session_id"] == first["session_id"]
    server.release.set()
    final = wait_status(store, "launch", {"completed", "failed", "blocked"})
    assert final["status"] == "completed", final
    assert final["attempt_id"] != first["attempt_id"]
    prompt = store.spec("launch").prompt
    assert all(
        sum(
            m["role"] == "user" and m.get("content") == prompt
            for m in request["messages"]
        )
        == 1
        for request in server.requests
    )


def test_detached_file_approval_requires_current_explicit_answer(cli, tmp_path):
    run, start, store, server, workspace = cli
    server.write_file = True
    assert start()[0] == 0
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = store.status("launch")
        if state["pending_requests"]:
            break
        assert state["alive"], state
        time.sleep(0.05)
    assert state["pending_requests"], state
    request = state["pending_requests"][0]
    assert request["kind"] == "review"
    assert "审批后写入" in json.dumps(request, ensure_ascii=False)
    assert not (workspace / "result.txt").exists()
    answer = tmp_path / "approve.json"
    answer.write_text('{"approved":true}', encoding="utf-8")
    request_id = next((store.path("launch") / "requests").glob("*.json")).stem
    assert run("answer", "launch", request_id, "--answer-file", str(answer))[0] == 0
    final = wait_status(store, "launch", {"completed", "failed", "blocked"})
    assert final["status"] == "completed", final
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "审批后写入"


def test_launcher_pid_can_differ_from_worker_pid(cli, monkeypatch):
    from reuleauxcoder.domain.jobs import JobSpec
    from reuleauxcoder.interfaces import jobs

    _, _, store, _, workspace = cli
    job_id, _ = store.create(
        JobSpec(
            str(workspace),
            "Complete this task",
            config=str(workspace.parent / "config.json"),
        ),
        "wrapper",
    )
    original = jobs.subprocess.Popen

    def wrapper(*args, **kwargs):
        process = original(*args, **kwargs)
        return SimpleNamespace(pid=process.pid + 100000, poll=process.poll)

    monkeypatch.setattr(jobs.subprocess, "Popen", wrapper)
    results = []
    monkeypatch.setattr(jobs, "_print", results.append)
    args = SimpleNamespace(
        detach=True, command="start", token_budget=None, max_seconds=None
    )
    assert jobs._launch(store, job_id, args) == 0
    assert results[0]["launch_state"] == "admitted"
    assert results[0]["launch_pid"] != results[0]["state"]["pid"]
    assert results[0]["launch_id"] == results[0]["state"]["launch_id"]
    assert (
        wait_status(store, job_id, {"completed", "failed", "blocked"})["status"]
        == "completed"
    )
