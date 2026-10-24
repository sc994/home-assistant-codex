"""Read-only Home Assistant verification for paired local Codex remote sessions.

The MCP client receives a boot-scoped socket capability, never Supervisor/API credentials.
Each check owns its evidence; remote sessions cannot save pending dashboards.
"""
from __future__ import annotations

from collections import deque
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import threading
import time
import tomllib
import uuid

from verification import Verification, RETENTION_SECONDS

BEGIN = '# BEGIN CODEX WORKER REMOTE VERIFICATION'
END = '# END CODEX WORKER REMOTE VERIFICATION'
MCP_NAME = 'home_assistant_remote'


def install_mcp_config(home):
    """Maintain just our marked MCP table, preserving all other user configuration."""
    path = Path(home) / 'config.toml'
    original = path.read_text() if path.exists() else ''
    # Refuse malformed/ambiguous configuration rather than overwrite user settings.
    tomllib.loads(original)
    if BEGIN in original or END in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            raise ValueError('Ambiguous remote MCP configuration markers')
        before, rest = original.split(BEGIN)
        managed, after = rest.split(END)
        if managed.count('[mcp_servers.') != 1 or after.strip():
            raise ValueError('Remote MCP configuration block was edited; preserve it for review')
        original = before.rstrip() + '\n'
    if MCP_NAME in tomllib.loads(original).get('mcp_servers', {}):
        raise ValueError('An unmanaged remote MCP server already exists')
    block = (f'\n{BEGIN}\n[mcp_servers.{MCP_NAME}]\n'
             'command = "python3"\nargs = ["/verification_mcp.py", "--remote"]\n'
             'enabled = true\nstartup_timeout_sec = 10\ntool_timeout_sec = 240\n'
             f'{END}\n')
    updated = original + block
    tomllib.loads(updated)
    if path.exists() and path.read_text() == updated:
        return
    temporary = path.with_name('config.toml.remote-pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(updated)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class EvidenceWorker:
    """Keep remote checks out of the task scheduler and delegate only trusted HA helpers."""

    def __init__(self, worker, root):
        self.worker = worker
        self.root = Path(root)
        self.lock = threading.RLock()
        self.tasks = {}

    def __getattr__(self, name):
        if name in {'ha_token', 'api_token', 'utc_now', 'redact', 'read_options',
                    'check_home_assistant_config'}:
            return getattr(self.worker, name)
        raise AttributeError(name)

    def task_cancellation_requested(self, task_id):
        return False

    def task_turns(self, task):
        return [task]

    def get_task_dir(self, task_id):
        # IDs are created by the worker, never supplied by a remote caller.
        return self.root / str(uuid.UUID(task_id))

    def update_task(self, task_id, **values):
        with self.lock:
            self.tasks[task_id].update(values)

    def _write_attachment_file(self, task_id, data, turn_id):
        name = uuid.uuid4().hex
        directory = self.get_task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / (name + '.png')
        with target.open('xb') as out:
            os.chmod(target, 0o600)
            out.write(data)
        return name, target, 'image/png'

    def attachment_record(self, task_id, attachment_id, target, mime, **values):
        return dict(attachment_id=attachment_id, path=target.name, mime=mime, **values)


class RemoteVerification(Verification):
    """Authenticate a boot-scoped local bridge, limit its work, and retain redacted evidence."""

    def __init__(self, worker, regular_engine):
        root = worker.DATA_ROOT / 'remote-verification'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        self.root = root
        self.source_worker = worker
        super().__init__(EvidenceWorker(worker, root / 'evidence'))
        self.execution_lock = regular_engine.execution_lock
        self.request_lock = threading.Lock()
        self.recent = deque()
        self.browsers = deque()
        self.remote_capability = secrets.token_urlsafe(32)
        key = root / 'capability'
        # Refuse symlinks; this file is replaced with a fresh capability every boot.
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(self.remote_capability)

    def cleanup(self):
        """Remove only our UUID evidence directories older than seven days."""
        root = self.root / 'evidence'
        if root.exists():
            for path in root.iterdir():
                try:
                    uuid.UUID(path.name)
                except ValueError:
                    continue
                if path.is_dir() and not path.is_symlink() and path.stat().st_mtime < time.time() - RETENTION_SECONDS:
                    shutil.rmtree(path)

    def dispatch(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Expected verification arguments')
        supplied = payload.get('capability')
        if not isinstance(supplied, str) or not supplied.isascii() or not hmac.compare_digest(supplied, self.remote_capability):
            raise ValueError('Invalid remote verification capability')
        settings = self.source_worker.DATA_ROOT / 'codex-home/app-server-daemon/settings.json'
        try:
            enabled = json.loads(settings.read_text()).get('remoteControlEnabled') is True
        except (OSError, ValueError, AttributeError):
            enabled = False
        if not enabled:
            raise ValueError('Remote control is disabled')
        allowed = {'capability', 'operation', 'entity_id', 'expected_state', 'attributes', 'path', 'save_pending'}
        if set(payload) - allowed or payload.get('operation') not in {'entity', 'config_check', 'logs', 'dashboard', 'dashboard_readback'}:
            raise ValueError('Unsupported remote verification request')
        if payload.get('save_pending') not in (None, False):
            raise ValueError('Remote verification cannot save dashboards; use a worker task')
        if not self.request_lock.acquire(blocking=False):
            return {'status': 'unavailable', 'message': 'Another remote verification is running'}
        task_id = None
        try:
            now = time.monotonic()
            while self.recent and now - self.recent[0] > 60:
                self.recent.popleft()
            if len(self.recent) >= 24:
                return {'status': 'unavailable', 'message': 'Remote verification limit reached; retry after one minute'}
            while self.browsers and now - self.browsers[0] > 60:
                self.browsers.popleft()
            if payload['operation'] == 'dashboard':
                if len(self.browsers) >= 4:
                    return {'status': 'unavailable', 'message': 'Remote dashboard limit reached; retry after one minute'}
                self.browsers.append(now)
            self.recent.append(now)
            task_id = str(uuid.uuid4())
            self.worker.tasks[task_id] = dict(task_id=task_id, status='running', current_turn_id=task_id)
            cap = self.begin(task_id)
            result = self.run(task_id, {k: v for k, v in payload.items() if k != 'capability'}, cap)
            directory = self.worker.get_task_dir(task_id)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            evidence = directory / 'result.json'
            with evidence.open('x') as out:
                os.chmod(evidence, 0o600)
                json.dump(result, out)
            return {**result, 'evidence_path': str(evidence)}
        finally:
            if task_id:
                self.end(task_id)
                self.worker.tasks.pop(task_id, None)
            self.request_lock.release()


def setup(worker, regular_engine):
    """Install MCP discovery before remote startup and expose an owner-only socket."""
    engine = RemoteVerification(worker, regular_engine)
    engine.cleanup()
    install_mcp_config(worker.DATA_ROOT / 'codex-home')
    engine.serve(engine.root / 'verification.sock')
    Path('/run/codex-remote-verification-ready').touch(mode=0o600)
    return engine
