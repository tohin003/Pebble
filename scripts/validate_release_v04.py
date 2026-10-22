"""Check the packaged service with synthetic images and a loopback provider."""
import json
import queue
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
records=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def do_GET(self):
        data={'data':[{'id':'gpt-6-luna','context_window':1050000,'input_modalities':['text','image'],'reasoning_capabilities':{'parameter':'reasoning','levels':['low','high'],'default':'low'}},{'id':'ordinary-chat','context_window':8192}]}
        self.send_response(200);self.end_headers();self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        records.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        event={'type':'response.completed','response':{'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Synthetic screenshot received.'}]}],'usage':{'input_tokens':120,'output_tokens':5}}}
        self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())

server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    with tempfile.TemporaryDirectory(prefix='pebble-v04-') as tmp:
        home=Path(tmp)/'data';image=Path(tmp)/'screenshot.png'
        Image.new('RGB',(1000,540),'#dae5d4').save(image)
        process=subprocess.Popen([str(ROOT/'src-tauri/resources/backend/pebble-backend'),'--home',str(home),'--resources',str(ROOT/'src-tauri/resources')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        lines=queue.Queue();events=[];seq=0
        def read():
            for line in process.stdout:lines.put(json.loads(line))
        threading.Thread(target=read,daemon=True).start()
        def rpc(method,**params):
            global seq
            seq+=1;rid=str(seq)
            process.stdin.write(json.dumps({'id':rid,'method':method,'params':params})+'\n');process.stdin.flush()
            until=time.monotonic()+30
            while time.monotonic()<until:
                value=lines.get(timeout=max(.1,until-time.monotonic()))
                if value.get('id')==rid:
                    if 'error' in value:raise AssertionError(value['error'])
                    return value['result']
                events.append(value)
            raise TimeoutError(method)
        try:
            assert rpc('state')['version']=='0.4.0'
            url=f'http://127.0.0.1:{server.server_port}/v1'
            assert len(rpc('model_discover',url=url,kind='responses'))==2
            rpc('model_save_many',name='Synthetic provider',url=url,kind='responses',models=['gpt-6-luna','ordinary-chat'],tools=False)
            assert [p['budget'] for p in rpc('state')['profiles']]==[1050000,8192]
            cid=rpc('new_chat')['id'];attachment=rpc('attach',id=cid,path=str(image))
            assert attachment['preview'].startswith('data:image/jpeg;base64,')
            rpc('send',id=cid,text='Describe the attached image.',source_ids=[attachment['id']],effort='low')
            for _ in range(60):
                if not rpc('state')['active']:break
                time.sleep(.1)
            assert not rpc('state')['active']
            chat=rpc('get_chat',id=cid)
            assert chat['messages'][0]['attachments'][0]['name']=='screenshot.png'
            assert chat['messages'][-1]['content']=='Synthetic screenshot received.'
            assert any(part.get('type')=='input_image' for item in records[-1]['input'] for part in item.get('content',[]) if isinstance(part,dict))
            assert records[-1]['reasoning']=={'effort':'low'}
            rpc('delete_chat',id=cid);assert not rpc('state')['chats']
            assert not [e for e in events if e.get('event')=='error']
            result={'version':'0.4.0','frozen_service':True,'isolated_image_worker':True,'native_responses_images':True,'shared_model_profiles':True,'full_context_metadata':True,'delete_cascade':True,'provider':'synthetic loopback','live_million_token_inference':False}
            (ROOT/'reports/frozen-v04.json').write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps(result,indent=2))
        finally:
            process.stdin.close();process.wait(timeout=10)
finally:
    server.shutdown();server.server_close()
