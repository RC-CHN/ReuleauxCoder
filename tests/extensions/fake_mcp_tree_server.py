"""Tiny MCP stdio server that owns a signal-resistant child process."""

from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys


def main() -> None:
    child_pid_path = Path(sys.argv[1])
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(300)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid_path.write_text(str(child.pid), encoding="utf-8")
    for line in sys.stdin:
        message = json.loads(line)
        request_id = message.get("id")
        if request_id is None:
            continue
        method = message.get("method")
        result = (
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "fake-tree-server", "version": "1"},
            }
            if method == "initialize"
            else {"tools": []}
            if method == "tools/list"
            else {}
        )
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    main()
