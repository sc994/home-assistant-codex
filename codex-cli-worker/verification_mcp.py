"""Stdio MCP bridge for task-scoped checks outside the shell network sandbox."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys

REMOTE_ROOT = Path("/data/remote-verification")

TOOL = {
    "name": "verify",
    "description": (
        "Check Home Assistant configuration, fresh entity state, Core logs, or dashboard configuration. "
        "Dashboard checks capture desktop/mobile screenshots; inspect their image_paths before visual claims. "
        "Blocked requests include reasons and approval availability; a policy block does not imply a write. "
        "save_pending saves only a dashboard edited in this turn when auto-save is enabled. "
        "No service calls, reloads, arbitrary URLs, or other apps' logs."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": ["entity", "config_check", "logs", "dashboard", "dashboard_readback"]},
            "entity_id": {"type": "string"}, "expected_state": {"type": "string"},
            "attributes": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            "path": {"type": "string", "description": "Local dashboard path, for example /lovelace/lights"},
            "save_pending": {"type": "boolean"},
        },
        "required": ["operation"],
    },
}

WORKER_TOOL = {
    "name": "worker_task",
    "description": (
        "Start a tracked Home Assistant configuration-editing task, read its status, continue it, or cancel it. "
        "Tasks use the add-on's existing backups, configuration checks and execution settings. "
        "Only tasks created by this tool are accessible. Start/continue may modify configuration; "
        "use only for user-authorized work. Poll status to verify completion, never claim queued work is done. "
        "For continue, pass current_turn_id from status as turn_id. Native permission decisions and "
        "browser permissions must be reviewed by the human in the add-on web UI; this tool cannot approve them."
    ),
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": ["start", "status", "continue", "cancel"]},
            "prompt": {"type": "string", "maxLength": 8000},
            "task_id": {"type": "string", "maxLength": 128},
            "message": {"type": "string", "maxLength": 8000},
            "turn_id": {"type": "string", "maxLength": 128},
        },
        "required": ["operation"],
    },
}


def verify(arguments, *, worker_task=False):
    """Forward only the supported fields and the process's temporary capability."""
    tool = WORKER_TOOL if worker_task else TOOL
    if not isinstance(arguments, dict) or set(arguments) - set(tool["inputSchema"]["properties"]):
        raise ValueError("Unsupported verification arguments")
    if arguments.get("operation") not in tool["inputSchema"]["properties"]["operation"]["enum"]:
        raise ValueError("Unsupported verification operation")
    remote = "--remote" in sys.argv[1:]
    if worker_task and not remote:
        raise ValueError("Worker tasks require the remote bridge")
    capability = ((REMOTE_ROOT / "capability").read_text().strip()
                  if remote else os.environ.get("HA_VERIFICATION_CAPABILITY", ""))
    payload = {**arguments, "capability": capability}
    if worker_task:
        payload['kind'] = 'worker_task'
    encoded = json.dumps(payload).encode() + b"\n"
    if len(encoded) > 16384:
        raise ValueError("Verification request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(230)
        client.connect(str(REMOTE_ROOT / "verification.sock") if remote else
                       os.environ.get("HA_VERIFICATION_SOCKET", "/data/verification.sock"))
        client.sendall(encoded)
        with client.makefile("rb") as response:
            raw = response.readline(128 * 1024 + 1)
    if not raw or len(raw) > 128 * 1024:
        raise ValueError("Invalid verification response")
    return json.loads(raw)


def respond(request):
    """Implement the MCP initialization, discovery and single-tool request surface."""
    if "--remote" in sys.argv[1:]:
        TOOL["description"] = ("Read-only Home Assistant verification for paired remote sessions: "
            "Core logs, entity states, configuration checks, dashboard readback and screenshots. "
            "Inspect returned image_paths before visual claims. No actions, reloads, arbitrary URLs "
            "or dashboard saves. Evidence stays locally for seven days. "
            "Use worker tasks for tracked edits, backups and save_pending.")
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "home-assistant-verification", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [TOOL, WORKER_TOOL] if "--remote" in sys.argv[1:] else [TOOL]}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            is_task = params.get("name") == WORKER_TOOL["name"] and "--remote" in sys.argv[1:]
            if params.get("name") != TOOL["name"] and not is_task:
                raise ValueError("Unknown verification tool")
            result = verify(params.get("arguments", {}), worker_task=True) if is_task else verify(params.get("arguments", {}))
        except (OSError, ValueError, TypeError):
            result = {"status": "unavailable", "message": "Verification request failed or the task has ended"}
        return {"content": [{"type": "text", "text": json.dumps(result)}],
                "isError": result.get("status") == "unavailable"}
    raise ValueError("Method not found")


def main():
    """Read bounded newline-delimited JSON-RPC; stdout contains protocol data only."""
    while raw := sys.stdin.buffer.readline(65537):
        if len(raw) > 65536:
            return 1
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"]}
            try:
                response["result"] = respond(request)
            except (ValueError, TypeError, AttributeError):
                response["error"] = {"code": -32601, "message": "Unsupported request"}
            print(json.dumps(response), flush=True)
        except (ValueError, UnicodeError):
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
