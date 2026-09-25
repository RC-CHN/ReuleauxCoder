"""Bounded isolated probes. No Agent, session restore, hooks or MCP startup."""

from __future__ import annotations

import json
import subprocess
import sys


def run_probe(layers: list[tuple[str, dict]], check: str, timeout: float = 20) -> dict:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "reuleauxcoder.services.config.probe", check],
            input=json.dumps(layers, allow_nan=False),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"check": check, "status": "unknown", "code": "timeout"}
    except OSError:
        return {"check": check, "status": "failed", "code": "probe_start_failed"}
    try:
        result = json.loads(completed.stdout)
        if result.get("status") not in ("passed", "failed", "unknown"):
            raise ValueError
        return {
            "check": check,
            "status": result["status"],
            "code": str(result.get("code", ""))[:64],
        }
    except (ValueError, AttributeError):
        # Never return provider stderr or exception text containing credentials.
        return {"check": check, "status": "failed", "code": "probe_failed"}


def main():
    from reuleauxcoder.services.config.validation import resolve_layers
    from reuleauxcoder.services.llm.factory import (
        build_llm_from_settings,
        probe_model_connection,
    )

    client = None
    try:
        config, issues = resolve_layers(json.load(sys.stdin))
        if config is None or any(issue.severity == "error" for issue in issues):
            result = {"status": "failed", "code": "invalid_config"}
        else:
            client = build_llm_from_settings(config, debug_trace=False)
            if sys.argv[1] == "model":
                probe_model_connection(client)
            result = {"status": "passed", "code": "ok"}
    except Exception as error:
        status = getattr(error, "status_code", None)
        if status in (401, 403):
            result = {"status": "failed", "code": "authentication_failed"}
        elif status in (400, 404, 422):
            result = {"status": "failed", "code": "request_rejected"}
        elif (
            status == 429
            or isinstance(error, (TimeoutError, ConnectionError))
            or type(error).__name__ in ("APIConnectionError", "APITimeoutError")
        ):
            result = {"status": "unknown", "code": "provider_unavailable"}
        else:
            result = {"status": "failed", "code": "initialization_failed"}
    finally:
        if client is not None:
            client.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
