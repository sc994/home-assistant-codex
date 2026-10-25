"""Remote task lifecycle uses real worker routes without giving callers API tokens."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_server import server
from remote_verification import RemoteVerification
import verification_mcp


class RemoteTaskTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (
            ('DATA_ROOT', self.root), ('tasks', {}), ('active_task_runners', set()),
            ('running_processes', {}), ('message_queue', []),
            ('TASK_STATE_FILE', self.root / 'index.json'), ('CODEX_HOME', self.root / 'codex'),
        ):
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, 'task_root', return_value=self.root / 'tasks'))
        self.stack.enter_context(patch.object(server, 'api_token', return_value='private-worker-secret'))
        self.stack.enter_context(patch.object(server, 'ha_token', return_value='private-ha-secret'))
        self.runner = self.stack.enter_context(patch.object(server, 'start_background_task'))
        self.engine = RemoteVerification(server, server.verification)
        self.settings = self.root / 'codex-home/app-server-daemon/settings.json'
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text('{"remoteControlEnabled":true}')
        self.session_id = '019fc242-910a-7c92-a17d-54c014e19fc4'
        sessions = server.CODEX_HOME / 'sessions'
        sessions.mkdir(parents=True)
        (sessions / f'rollout-{self.session_id}.jsonl').write_text('{}\n')

    def call(self, operation, **arguments):
        return self.engine.dispatch(dict(capability=self.engine.remote_capability,
            kind='worker_task', operation=operation, **arguments))

    def start(self):
        result = self.call('start', prompt='Update Viewport actions only while the TV is on')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'queued')
        return result['task_id']

    def finish(self, task_id, **values):
        server.update_task(task_id, status='completed', session_id=self.session_id, **values)
        server.active_task_runners.discard(task_id)

    def test_start_persists_origin_before_runner_and_survives_restart(self):
        def runner(task_id, prompt):
            saved = json.loads((server.get_task_dir(task_id) / 'task.json').read_text())
            self.assertIs(saved['remote_worker_task'], True)
            self.assertEqual(saved['prompt'], prompt)
        self.runner.side_effect = runner
        task_id = self.start()
        self.finish(task_id, summary='Updated and validated')
        server.tasks.clear()
        server.load_task_index()
        result = self.call('status', task_id=task_id)
        self.assertEqual(result['task']['summary'], 'Updated and validated')
        self.assertNotIn('prompt', result['task'])
        self.assertNotIn('session_id', result['task'])

    def test_continue_current_turn_and_cancel_use_worker_routes(self):
        task_id = self.start()
        self.finish(task_id)
        turn_id = self.call('status', task_id=task_id)['task']['current_turn_id']
        with self.assertRaises(ValueError):
            self.call('continue', task_id=task_id, turn_id='stale', message='Follow up')
        result = self.call('continue', task_id=task_id, turn_id=turn_id, message='Check again')
        self.assertTrue(result['ok'], result)
        self.assertNotEqual(server.tasks[task_id]['current_turn_id'], turn_id)
        self.runner.assert_called_with(task_id, server.tasks[task_id]['prompt'],
                                       session_id=self.session_id, reply='Check again')
        with patch.object(server, 'start_next_queued'), \
             patch.object(server, 'fire_ha_event', return_value=(True, '')), \
             patch.object(server, 'notify'), patch.object(server, 'refresh_usage_status_async'):
            self.assertEqual(self.call('cancel', task_id=task_id)['status'], 'cancelled')

    def test_no_access_to_ui_chats_or_approval_endpoints(self):
        server.tasks['local-chat'] = {'task_id': 'local-chat', 'status': 'completed'}
        for operation in ('status', 'cancel', 'continue'):
            extra = {'turn_id': 'turn', 'message': 'go'} if operation == 'continue' else {}
            with self.assertRaises(ValueError):
                self.call(operation, task_id='local-chat', **extra)
        for arguments in (
            {'operation': 'start', 'prompt': 'go', 'chat_settings': {'sandbox_mode': 'danger-full-access'}},
            {'operation': 'approve', 'task_id': 'local-chat'},
            {'operation': 'start', 'prompt': 'go', 'url': 'http://example.com'},
            {'operation': 'status', 'task_id': '../secrets'},
            {'operation': 'start', 'prompt': 'x' * 8001},
            {'operation': 'start', 'prompt': False},
        ):
            operation = arguments.pop('operation')
            with self.assertRaises(ValueError):
                self.call(operation, **arguments)
        self.runner.assert_not_called()

    def test_authentication_revocation_and_busy_worker(self):
        with self.assertRaises(ValueError):
            self.engine.dispatch({'capability': 'wrong', 'kind': 'worker_task',
                                  'operation': 'start', 'prompt': 'go'})
        self.settings.write_text('{"remoteControlEnabled":false}')
        with self.assertRaises(ValueError):
            self.call('start', prompt='go')
        self.settings.write_text('{"remoteControlEnabled":true}')
        self.start()
        busy = self.call('start', prompt='another task')
        self.assertEqual(busy['status'], 'unavailable')
        self.assertIn('already running', busy['message'])
        self.assertEqual(self.runner.call_count, 1)

    def test_results_redact_credentials_bound_details_and_keep_approvals_human(self):
        task_id = self.start()
        server.update_task(task_id, status='running',
            details='private-worker-secret private-ha-secret ' + 'x' * 20000,
            approvals=[{'status': 'pending', 'command': 'private command'}])
        result = self.call('status', task_id=task_id)
        encoded = json.dumps(result)
        self.assertNotIn('private-worker-secret', encoded)
        self.assertNotIn('private-ha-secret', encoded)
        self.assertNotIn('private command', encoded)
        self.assertTrue(result['task']['permission_required'])
        self.assertIn('web UI', result['task']['approval_hint'])
        self.assertLess(len(result['task']['details']), 17000)

    def test_unicode_status_fits_socket_response_limit(self):
        task_id = self.start()
        server.update_task(task_id, details='☃' * 20000,
            config_check={'result': 'valid', 'errors': ''},
            backups=[{'path': '☃' * 1000, 'status': 'saved', 'copy': '☃' * 1000}] * 40)
        result = self.call('status', task_id=task_id)
        self.assertLessEqual(len(json.dumps(result).encode()), 60000)
        self.assertTrue(result['task']['backups_truncated'])
        self.assertEqual(result['task']['config_check']['result'], 'valid')

    def test_worker_discovery_is_remote_only(self):
        for argv, expected in ((['mcp'], ['verify']), (['mcp', '--remote'], ['verify', 'worker_task'])):
            with patch.object(sys, 'argv', argv):
                result = verification_mcp.respond({'method': 'tools/list'})
                self.assertEqual([tool['name'] for tool in result['tools']], expected)
        with patch.object(sys, 'argv', ['mcp']):
            result = verification_mcp.respond({'method': 'tools/call', 'params':
                {'name': 'worker_task', 'arguments': {'operation': 'start', 'prompt': 'go'}}})
            self.assertTrue(result['isError'])

    def test_real_stdio_and_socket_start_and_poll(self):
        sock = self.engine.serve(self.engine.root / 'verification.sock')
        self.addCleanup(sock.server_close)
        self.addCleanup(sock.shutdown)
        def request(arguments):
            code = ('import sys; from pathlib import Path; import verification_mcp as m; '
                    'm.REMOTE_ROOT=Path(sys.argv[1]); sys.argv=["mcp", "--remote"]; m.main()')
            reply = subprocess.run([sys.executable, '-c', code, str(self.engine.root)],
                input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': 'worker_task', 'arguments': arguments}}) + '\n',
                text=True, capture_output=True, timeout=10,
                cwd=Path(verification_mcp.__file__).parent)
            self.assertEqual(reply.returncode, 0, reply.stderr)
            self.assertNotIn(self.engine.remote_capability, reply.stdout)
            result = json.loads(reply.stdout)['result']
            self.assertFalse(result['isError'], result)
            return json.loads(result['content'][0]['text'])
        task_id = request({'operation': 'start', 'prompt': 'Update the automation'})['task_id']
        self.finish(task_id, summary='Validated')
        self.assertEqual(request({'operation': 'status', 'task_id': task_id})['task']['summary'], 'Validated')

    def test_public_route_cannot_set_remote_origin_and_stale_turn_is_atomic(self):
        with server.app.test_client() as client:
            result = client.post('/tasks', json={'prompt': 'local', 'remote_worker_task': True},
                headers={'X-Codex-Worker-Token': 'private-worker-secret'}).get_json()
            task_id = result['task_id']
            self.assertNotIn('remote_worker_task', server.tasks[task_id])
            self.finish(task_id)
            result = client.post(f'/tasks/{task_id}/continue', json={'message': 'follow up'},
                headers={'X-Codex-Worker-Token': 'private-worker-secret'},
                environ_overrides={'codex.remote_turn_id': 'stale'})
            self.assertEqual(result.status_code, 409)


if __name__ == '__main__':
    unittest.main()
