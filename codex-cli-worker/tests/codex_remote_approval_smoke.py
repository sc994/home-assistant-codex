"""Prove remote MCP human approval works with the pinned CLI and no real account."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def main():
    binary = os.environ.get('HA_TEST_CODEX', '/usr/local/bin/codex')
    with tempfile.TemporaryDirectory(prefix='remote-approval-smoke-') as tmp:
        root = Path(tmp)
        home = root / 'codex-home'
        with patch.multiple(server, CODEX_HOME=home, DATA_ROOT=root, AUTH_QR_DIR=root/'qr',
                            SCHEMA_PATH=root/'schema.json', CODEX_CONFIG_PATH=home/'config.toml'), \
             patch.object(server, 'task_root', return_value=root/'tasks'), \
             patch.object(server, 'api_token', return_value='fixture'):
            server.ensure_runtime_files()
        generated = (home/'config.toml').read_text()
        receipt = root/'executed'
        fixture = root/'mcp.py'
        fixture.write_text('import sys; sys.path.insert(0, '+repr(str(Path(server.__file__).parent))+')\n'
                           'import verification_mcp as m\nfrom pathlib import Path\n'
                           'def verify(args):\n    Path('+repr(str(receipt))+').write_text("executed")\n'
                           '    return {"status":"observed","message":"fixture logs"}\n'
                           'm.verify=verify\nm.main()\n')
        stage = 0
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                nonlocal stage
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if stage == 0:
                    functions=[]
                    for tool in request.get('tools',[]):
                        if tool.get('type')=='namespace':
                            functions.extend((tool['name'],t) for t in tool.get('tools',[]))
                        else:functions.append((None,tool))
                    selected=next(((ns,t) for ns,t in functions if 'verify' in t.get('name','')),None)
                    assert selected,[(ns,t.get('name')) for ns,t in functions]
                    ns,tool=selected
                    item={'type':'function_call','call_id':'logs','name':tool['name'],
                          'arguments':json.dumps({'operation':'logs'})}
                    if ns:item['namespace']=ns
                else:
                    item={'type':'message','role':'assistant','status':'completed',
                          'content':[{'type':'output_text','text':'Fixture done','annotations':[]}]}
                stage+=1
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                for event in [
                    {'type':'response.created','response':{'id':f'resp_{stage}'}},
                    {'type':'response.output_item.added','output_index':0,'item':item},
                    {'type':'response.output_item.done','output_index':0,'item':item},
                    {'type':'response.completed','response':{'id':f'resp_{stage}','status':'completed','output':[item]}},
                ]:
                    self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n').encode())
                self.wfile.flush()
        provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        threading.Thread(target=provider.serve_forever,daemon=True).start()
        settings={
            'model_provider':'fixture','model_providers.fixture.name':'Fixture',
            'model_providers.fixture.base_url':f'http://127.0.0.1:{provider.server_port}/v1',
            'model_providers.fixture.wire_api':'responses','model_providers.fixture.requires_openai_auth':False,
            'mcp_servers.fixture.command':sys.executable,'mcp_servers.fixture.args':[str(fixture)],
            'mcp_servers.fixture.required':True,'mcp_servers.fixture.tools.verify.approval_mode':'prompt',
        }
        args=[a for k,v in settings.items() for a in ['-c',k+'='+json.dumps(v)]]
        try:
            for policy,decision in [('never','accept'),('on-request','accept'),('on-request','decline')]:
                stage=0;receipt.unlink(missing_ok=True)
                (home/'config.toml').write_text(generated.replace('approval_policy = "on-request"',f'approval_policy = "{policy}"'))
                approvals=[];messages=queue.Queue()
                with (root/'stderr').open('w+') as errors:
                    proc=subprocess.Popen([binary,'app-server',*args],env={**os.environ,'CODEX_HOME':str(home)},
                        cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,text=True,bufsize=1)
                    def reader():
                        for line in proc.stdout:
                            messages.put(json.loads(line))
                    thread=threading.Thread(target=reader,daemon=True);thread.start()
                    def send(data):proc.stdin.write(json.dumps(data)+'\n');proc.stdin.flush()
                    send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'fixture','version':'1'},'capabilities':{'experimentalApi':True}}})
                    deadline=time.monotonic()+35
                    try:
                        while time.monotonic()<deadline:
                            try:msg=messages.get(timeout=1)
                            except queue.Empty:continue
                            if 'method' in msg and 'id' in msg:
                                approvals.append(msg['method'])
                                assert msg['method']=='mcpServer/elicitation/request',msg
                                send({'id':msg['id'],'result':{'action':decision}})
                            elif msg.get('id')==1:
                                assert 'result' in msg,msg
                                send({'method':'initialized'})
                                send({'id':2,'method':'thread/start','params':{'cwd':str(root),'model':'gpt-5.1-codex','sandbox':'workspace-write'}})
                            elif msg.get('id')==2:
                                assert 'result' in msg,msg
                                result=msg['result']
                                assert result['approvalPolicy']==policy,result
                                assert result['sandbox']['type']=='workspaceWrite',result
                                send({'id':3,'method':'turn/start','params':{'threadId':result['thread']['id'],
                                      'input':[{'type':'text','text':'Read fixture logs'}]}})
                            elif msg.get('method')=='turn/completed':break
                        else:raise AssertionError('Fixture timed out')
                        if policy=='never':assert not approvals and not receipt.exists(),approvals
                        elif decision=='accept':assert approvals and receipt.exists(),approvals
                        else:assert approvals and not receipt.exists(),approvals
                        print('PASS',policy,decision,'approval prompts:',len(approvals),'tool executed:',receipt.exists())
                    except Exception:
                        errors.seek(0);print(errors.read()[-2500:],file=sys.stderr);raise
                    finally:
                        proc.terminate();proc.wait(timeout=5);proc.stdin.close();proc.stdout.close();thread.join(2)
        finally:provider.shutdown();provider.server_close()


if __name__=='__main__': main()
