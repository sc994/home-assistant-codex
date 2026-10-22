"""Exercise saved remote-control preference and startup failures without a real daemon."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

WORKER = Path(__file__).resolve().parents[1]


class RemoteControlStartupTests(unittest.TestCase):
    """Startup restores the saved preference and remains independent of the worker."""

    def run_startup(self, settings: str | None, *, failures: int = 0,
                    wait_for_worker: bool = False) -> tuple[list[dict], str]:
        """Run the real startup block with only private dependency paths substituted."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preference = root / "settings.json"
            if settings is not None:
                preference.write_text(settings)
            calls = root / "calls.jsonl"
            ready = root / "worker-ready"
            fake = root / "codex"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys, time\n"
                "calls = pathlib.Path(os.environ['TEST_CALLS'])\n"
                "previous = calls.read_text().splitlines() if calls.exists() else []\n"
                "if os.environ['TEST_WAIT'] == '1':\n"
                "    for attempt in range(100):\n"
                "        if pathlib.Path(os.environ['TEST_READY']).exists(): break\n"
                "        time.sleep(0.01)\n"
                "    else: sys.exit(99)\n"
                "with calls.open('a') as out:\n"
                "    out.write(json.dumps({'args': sys.argv[1:], 'path': os.environ['PATH'], 'inherited_worker_auth': [key for key in ['SUPERVISOR_TOKEN', 'HASSIO_TOKEN', 'HA_TOKEN', 'HA_URL', 'HA_VERIFICATION_CAPABILITY', 'HA_DATA_CAPABILITY', 'HA_DATA_SOCKET'] if key in os.environ]}) + '\\n')\n"
                "sys.exit(1 if len(previous) < int(os.environ['TEST_FAILURES']) else 0)\n"
            )
            fake.chmod(0o755)
            startup = (WORKER / "run.sh").read_text()
            block = startup[startup.index("# The enabled preference"):
                            startup.index('echo "Codex CLI Worker starting"')]
            block = block.replace("/data/codex-home/app-server-daemon/settings.json", str(preference))
            block = block.replace("/usr/local/bin/codex", str(fake))
            environment = {
                "PATH": os.defpath,
                **{key: "test-only" for key in ["SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HA_TOKEN", "HA_URL", "HA_VERIFICATION_CAPABILITY", "HA_DATA_CAPABILITY", "HA_DATA_SOCKET"]},
                "TEST_CALLS": str(calls), "TEST_READY": str(ready),
                "TEST_FAILURES": str(failures),
                "TEST_WAIT": "1" if wait_for_worker else "0",
            }
            script = (
                "set -euo pipefail\n"
                "bashio::log.info() { printf 'info: %s\\n' \"$*\"; }\n"
                "bashio::log.warning() { printf 'warning: %s\\n' \"$*\"; }\n"
                "sleep() { :; }\n"
                + next(line for line in startup.splitlines() if line.startswith('export PATH='))
                + "\n" + block + f"\ntouch '{ready}'\nwait\n"
            )
            result = subprocess.run(["bash", "-c", script], env=environment,
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(ready.exists())
            records = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            return records, result.stdout

    def test_missing_disabled_or_invalid_preference_does_not_start(self) -> None:
        """Only an explicitly enabled boolean restores remote access."""
        for setting in [None, "not JSON", "[]", "{}", '{"remoteControlEnabled": false}',
                        '{"remoteControlEnabled": "true"}']:
            with self.subTest(setting=setting):
                records, _ = self.run_startup(setting)
                self.assertEqual(records, [])

    def test_saved_enabled_preference_restores_remote_control(self) -> None:
        """A previously enabled daemon starts through the trusted wrapper PATH."""
        records, _ = self.run_startup('{"remoteControlEnabled": true}')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["args"], ["remote-control", "start", "--json"])
        self.assertEqual(records[0]["path"].split(":")[0], "/opt/codex-sandbox")
        self.assertEqual(records[0]["inherited_worker_auth"], [])

    def test_transient_failure_retries(self) -> None:
        """A temporary startup failure can recover without restarting the app."""
        records, _ = self.run_startup('{"remoteControlEnabled": true}', failures=2)
        self.assertEqual(len(records), 3)

    def test_exhausted_retries_leave_worker_available(self) -> None:
        """Persistent remote failure is bounded and does not fail worker startup."""
        records, output = self.run_startup('{"remoteControlEnabled": true}', failures=99)
        self.assertEqual(len(records), 6)
        self.assertIn("warning:", output)

    def test_remote_start_does_not_wait_before_worker_starts(self) -> None:
        """Even a remote process that waits on worker readiness runs in the background."""
        records, _ = self.run_startup('{"remoteControlEnabled": true}', wait_for_worker=True)
        self.assertEqual(len(records), 1)
