"""Deterministic stdio LSP server used by protocol and lifecycle tests."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


class FakeLspServer:
    def __init__(
        self,
        *,
        mode: str,
        log_path: Path,
        first_save_gate: Path | None,
    ) -> None:
        self.mode = mode
        self.log_path = log_path
        self.first_save_gate = first_save_gate
        self.documents: dict[str, dict[str, Any]] = {}
        self.result_versions: dict[str, int] = {}
        self.result_ids: dict[str, str] = {}
        self.save_count = 0
        self.trace_sequence = 0

    def run(self) -> None:
        while message := self._read_message():
            method = message.get("method")
            trace = self._request_trace(message)
            self._log(
                direction="recv",
                method=method,
                request_id=message.get("id"),
                **trace,
            )
            if method == "initialize":
                capabilities: dict[str, Any] = {
                    "textDocumentSync": {
                        "openClose": True,
                        "change": 1,
                        "save": True,
                    }
                }
                if self.mode == "pull":
                    capabilities["diagnosticProvider"] = {
                        "identifier": "reuleauxcoder-fake",
                        "interFileDependencies": False,
                        "workspaceDiagnostics": False,
                    }
                self._respond(
                    message,
                    {
                        "capabilities": capabilities,
                        "serverInfo": {"name": "reuleauxcoder-fake-lsp"},
                    },
                )
            elif method == "textDocument/didOpen":
                document = message["params"]["textDocument"]
                self.documents[document["uri"]] = {
                    "text": document.get("text", ""),
                    "version": int(document.get("version", 1)),
                }
                if self.mode == "push":
                    self._publish(document["uri"])
            elif method == "textDocument/didChange":
                document = message["params"]["textDocument"]
                change = message["params"]["contentChanges"][-1]
                self.documents[document["uri"]] = {
                    "text": change.get("text", ""),
                    "version": int(document.get("version", 1)),
                }
                if self.mode == "push":
                    self._publish(document["uri"])
            elif method == "textDocument/didSave":
                if self.mode == "save-only":
                    self.save_count += 1
                    if self.save_count == 1 and self.first_save_gate is not None:
                        self._log(direction="state", method="first_save_blocked")
                        deadline = time.monotonic() + 5.0
                        while (
                            not self.first_save_gate.exists()
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)
                        self._log(direction="state", method="first_save_released")
                    self._publish(message["params"]["textDocument"]["uri"])
            elif method == "textDocument/diagnostic":
                self._respond(message, self._pull(message["params"]))
            elif method == "shutdown":
                self._respond(message, None)
            elif method == "exit":
                return
            elif "id" in message:
                self._respond(message, None)

    def _pull(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params["textDocument"]["uri"]
        document = self.documents.get(uri, {"text": "", "version": 0})
        version = int(document["version"])
        previous_result_id = params.get("previousResultId")
        current_result_id = self.result_ids.get(uri)
        if (
            previous_result_id
            and previous_result_id == current_result_id
            and self.result_versions.get(uri) == version
        ):
            return {"kind": "unchanged", "resultId": current_result_id}

        result_id = f"result-{version}"
        self.result_ids[uri] = result_id
        self.result_versions[uri] = version
        return {
            "kind": "full",
            "resultId": result_id,
            "items": self._diagnostics(str(document["text"])),
        }

    def _publish(self, uri: str) -> None:
        document = self.documents.get(uri, {"text": "", "version": 0})
        diagnostics = self._diagnostics(str(document["text"]))
        self._send(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": uri,
                    "version": int(document["version"]),
                    "diagnostics": diagnostics,
                },
            },
            method="textDocument/publishDiagnostics",
            uri=uri,
            version=int(document["version"]),
            item_count=len(diagnostics),
        )

    @staticmethod
    def _diagnostics(text: str) -> list[dict[str, Any]]:
        marker = "FAKE_LSP_ERROR:"
        message = next(
            (
                line.split(marker, 1)[1].strip()
                for line in text.splitlines()
                if marker in line
            ),
            None,
        )
        if message is None:
            return []
        return [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 1},
                },
                "message": message or "deterministic broken source",
                "severity": 1,
                "source": "reuleauxcoder-fake-lsp",
            }
        ]

    def _respond(self, request: dict[str, Any], result: Any) -> None:
        result_trace: dict[str, Any] = {}
        if isinstance(result, dict) and result.get("kind") in {"full", "unchanged"}:
            result_trace = {
                "kind": result["kind"],
                "result_id": result.get("resultId"),
                "item_count": len(result.get("items", [])),
            }
        self._send(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            method=f"response:{request.get('method')}",
            **result_trace,
        )

    def _send(self, message: dict[str, Any], *, method: str, **trace: Any) -> None:
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()
        self._log(
            direction="send",
            method=method,
            request_id=message.get("id"),
            **trace,
        )

    @staticmethod
    def _request_trace(message: dict[str, Any]) -> dict[str, Any]:
        params = message.get("params")
        if not isinstance(params, dict):
            return {}
        document = params.get("textDocument")
        if not isinstance(document, dict):
            return {}
        trace = {
            "uri": document.get("uri"),
            "version": document.get("version"),
        }
        if "previousResultId" in params:
            trace["previous_result_id"] = params["previousResultId"]
        return trace

    @staticmethod
    def _read_message() -> dict[str, Any] | None:
        content_length = 0
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line in {b"\n", b"\r\n"}:
                break
            name, _, value = line.decode("ascii", errors="replace").partition(":")
            if name.lower() == "content-length":
                content_length = int(value.strip())
        if content_length <= 0:
            return None
        body = sys.stdin.buffer.read(content_length)
        if len(body) != content_length:
            return None
        return json.loads(body.decode("utf-8"))

    def _log(self, **event: Any) -> None:
        self.trace_sequence += 1
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"sequence": self.trace_sequence, **event}, sort_keys=True)
                + "\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("save-only", "push", "pull"), required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--block-first-save-until", type=Path)
    args = parser.parse_args()
    FakeLspServer(
        mode=args.mode,
        log_path=args.log,
        first_save_gate=args.block_first_save_until,
    ).run()


if __name__ == "__main__":
    main()
