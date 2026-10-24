"""Paired remote checks reuse HA verification without task or Supervisor credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from remote_verification import RemoteVerification, install_mcp_config
from verification import Verification
import verification_mcp


class RemoteVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = Mock()
        self.worker.DATA_ROOT = self.root
        self.worker.lock = threading.RLock()
        self.worker.read_options.return_value = {'browser_verification': True}
        self.worker.redact.side_effect = lambda text: text.replace('test-secret', '[redacted]')
        self.worker.utc_now.return_value = '2026-10-23T08:00:00+00:00'
        self.regular = Verification(self.worker)
        self.engine = RemoteVerification(self.worker, self.regular)
        self.settings = self.root / 'codex-home/app-server-daemon/settings.json'
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text('{"remoteControlEnabled": true}')

    def call(self, **payload):
        return self.engine.dispatch(dict(capability=self.engine.remote_capability, **payload))

    def test_all_operations_use_existing_engine_and_keep_evidence(self):
        with patch.object(self.engine, 'core', return_value={'text': 'test-secret'}), \
             patch.object(self.engine, 'entity', return_value={'status': 'observed', 'state': 'on'}), \
             patch.object(self.engine, 'dashboard_readback', return_value={'status': 'observed'}), \
             patch.object(self.engine, 'browser', return_value={'status': 'captured', 'image_paths': []}):
            self.worker.check_home_assistant_config.return_value = {'result': 'valid'}
            for operation in ('logs', 'entity', 'config_check', 'dashboard_readback', 'dashboard'):
                result = self.call(operation=operation)
                self.assertNotEqual(result['status'], 'unavailable', result)
                evidence = Path(result['evidence_path'])
                self.assertTrue(evidence.is_file())
                self.assertNotIn('test-secret', evidence.read_text())
                self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.engine.worker.tasks)
        self.assertFalse(self.engine.capabilities)
        self.assertIs(self.engine.execution_lock, self.regular.execution_lock)

    def test_reject_wrong_capability_disabled_control_writes_and_extra_fields(self):
        with patch.object(self.engine, 'core') as core:
            for payload in ({'capability': 'wrong', 'operation': 'logs'},
                            {'operation': 'service'}, {'operation': 'dashboard', 'save_pending': True},
                            {'operation': 'logs', 'target': 'supervisor'}):
                with self.assertRaises(ValueError):
                    self.engine.dispatch({'capability': self.engine.remote_capability, **payload})
            self.settings.write_text('{"remoteControlEnabled": false}')
            with self.assertRaises(ValueError): self.call(operation='logs')
            core.assert_not_called()

    def test_rate_limits_and_worker_concurrency(self):
        with patch.object(self.engine, 'entity', return_value={'status': 'observed'}):
            for _ in range(24): self.call(operation='entity')
            self.assertEqual(self.call(operation='entity')['status'], 'unavailable')
        self.engine.recent.clear()
        self.regular.execution_lock.acquire()
        try:self.assertEqual(self.call(operation='logs')['status'], 'unavailable')
        finally:self.regular.execution_lock.release()
        with patch.object(self.engine, 'browser', return_value={'status': 'captured'}):
            for _ in range(4):self.call(operation='dashboard')
            self.assertEqual(self.call(operation='dashboard')['status'], 'unavailable')

    def test_socket_and_stdio_mcp_round_trip(self):
        sock = self.engine.serve(self.root / 'remote-verification/verification.sock')
        self.addCleanup(sock.server_close)
        self.addCleanup(sock.shutdown)
        with patch.object(self.engine, 'core', return_value={'text': 'test-secret'}):
            # Real MCP subprocess and Unix socket; only fake the private directory, never auth.
            code = ('import sys; from pathlib import Path; import verification_mcp as m; '
                    'm.REMOTE_ROOT=Path(sys.argv[1]); sys.argv=["mcp", "--remote"]; m.main()')
            result = subprocess.run([sys.executable, '-c', code, str(self.root/'remote-verification')],
                input=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call',
                                  'params':{'name':'verify','arguments':{'operation':'logs'}}})+'\n',
                text=True, capture_output=True, cwd=Path(verification_mcp.__file__).parent, timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            reply=json.loads(result.stdout)['result']
            self.assertFalse(reply['isError'])
            self.assertIn('[redacted]',reply['content'][0]['text'])
            self.assertNotIn(self.engine.remote_capability,result.stdout)
            self.assertEqual((self.root/'remote-verification/verification.sock').stat().st_mode & 0o777,0o600)

    def test_boot_capability_rotation_and_screenshot_paths(self):
        original=self.engine.remote_capability
        second=RemoteVerification(self.worker,self.regular)
        self.assertNotEqual(original,second.remote_capability)
        with self.assertRaises(ValueError):second.dispatch({'operation':'logs','capability':original})
        task_id='11111111-1111-1111-1111-111111111111'
        attachment_id,path,mime=second.worker._write_attachment_file(task_id,b'png',task_id)
        record=second.worker.attachment_record(task_id,attachment_id,path,mime,origin='verification')
        self.assertEqual((second.worker.get_task_dir(task_id)/record['path']).read_bytes(),b'png')

    def test_config_preserves_settings_is_idempotent_and_refuses_collision(self):
        home=self.root/'config-test';home.mkdir()
        path=home/'config.toml';original='model = "test-model"\n[mcp_servers.existing]\ncommand = "example"\n'
        path.write_text(original)
        install_mcp_config(home);first=path.read_text();install_mcp_config(home)
        self.assertEqual(first,path.read_text());self.assertIn(original.strip(),first)
        self.assertNotIn('capability',first)
        path.write_text('[mcp_servers.home_assistant_remote]\ncommand="custom"\n')
        with self.assertRaises(ValueError):install_mcp_config(home)
        self.assertIn('custom',path.read_text())
