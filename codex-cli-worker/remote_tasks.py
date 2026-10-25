"""Bounded remote access to the existing tracked worker task lifecycle.

Called only after RemoteVerification authenticates the boot capability and checks
the saved remote-control preference. Worker credentials never leave this process.
Native approvals and browser permissions remain human-only in the Ingress UI.
"""
from __future__ import annotations

import json
import re


def redact_result(worker, value):
    """Redact string values without rewriting JSON syntax or returning credentials."""
    if isinstance(value, dict):
        return {key: redact_result(worker, item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_result(worker, item) for item in value]
    if isinstance(value, str):
        value = worker.redact(value)
        for secret in (worker.api_token(), worker.ha_token()):
            if secret:
                value = value.replace(secret, '[redacted]')
    return value


def dispatch(worker, payload):
    operation = payload.get('operation')
    fields = {
        'start': {'prompt'}, 'status': {'task_id'},
        'continue': {'task_id', 'message', 'turn_id'}, 'cancel': {'task_id'},
    }
    if operation not in fields or set(payload) - (fields[operation] | {'operation'}):
        raise ValueError('Unsupported worker task request')
    for field in fields[operation]:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{field} must be non-empty text')
        if len(value) > (8000 if field in {'prompt', 'message'} else 128):
            raise ValueError(f'{field} is too long')
    if operation == 'start':
        route, method, body = '/tasks', 'POST', {'prompt': payload['prompt']}
    else:
        task_id = payload['task_id']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', task_id):
            raise ValueError('Invalid task ID')
        with worker.lock:
            task = worker.tasks.get(task_id)
            if not task or task.get('remote_worker_task') is not True:
                raise ValueError('Task was not created through remote worker access')
            if operation == 'status':
                result = worker.task_payload(task, summary=True, private=False)
                # No prompts, session IDs, logs, approval payloads or arbitrary files.
                result.update({key: task.get(key, '') for key in
                               ('current_turn_id', 'details', 'choices')})
                result['details'] = str(result['details'])[:16000]
                result['question'] = str(result.get('question', ''))[:4000]
                result['choices'] = [str(item)[:1000] for item in (result['choices'] or [])[:3]]
                check = task.get('config_check') or {}
                result['config_check'] = {key: str(check.get(key, ''))[:4000]
                                          for key in ('result', 'errors')}
                backups = task.get('backups') or []
                result['backups'] = [{key: str(item.get(key, ''))[:1000]
                                      for key in ('path', 'status', 'copy')}
                                     for item in backups[:30] if isinstance(item, dict)]
                result['backups_truncated'] = len(backups) > 30
                if result.get('permission_required'):
                    result['approval_hint'] = 'Review the pending permission request in the add-on web UI.'
                result = redact_result(worker, {'ok': True, 'task': result})
                # The socket/MCP response is bounded in bytes, including Unicode
                # escaping and redaction expansion, rather than just characters.
                while len(json.dumps(result).encode()) > 60000:
                    task_result = result['task']
                    if task_result['backups']:
                        task_result['backups'].pop()
                        task_result['backups_truncated'] = True
                    else:
                        candidates = [(task_result, key) for key in ('details', 'question')]
                        candidates.append((task_result['config_check'], 'errors'))
                        candidates.extend((task_result['choices'], index)
                                          for index in range(len(task_result['choices'])))
                        container, key = max(candidates, key=lambda item: len(item[0][item[1]]))
                        container[key] = container[key][:len(container[key]) // 2]
                        task_result['result_truncated'] = True
                return result
            if operation == 'continue' and task.get('current_turn_id') != payload['turn_id']:
                raise ValueError('Task turn changed; read its status before continuing')
        route = f'/tasks/{task_id}/{operation}'
        method = 'POST'
        body = {'message': payload['message']} if operation == 'continue' else {}
    # Use the same authenticated routes as the integration. The internal marker
    # is an environ value, never a client-supplied header or JSON field.
    with worker.app.test_client() as client:
        response = client.open(route, method=method, json=body,
            headers={'X-Codex-Worker-Token': worker.api_token()},
            environ_overrides={'codex.remote_worker_task': True,
                               'codex.remote_turn_id': payload.get('turn_id')})
        result = response.get_json()
    if not isinstance(result, dict):
        return {'status': 'unavailable', 'message': 'Worker task request failed'}
    if response.status_code >= 400:
        return {'status': 'unavailable', 'message': worker.redact(str(result.get('error', 'Worker task request failed')))}
    return redact_result(worker, result)
