"""Real local-model regression for a deeper-thinking website task."""
from pathlib import Path
import json
import tempfile
import time
import urllib.request
import sys
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "backend"), str(Path(__file__).resolve().parents[1] / "backend/vendor")]
from pebble.app import App
from pebble.permissions import DEFAULT


def main():
    root_dir=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='pebble-website-qa-') as tmp:
        root=Path(tmp);project=root/'project';project.mkdir()
        events=[]
        app=App(root/'data',root_dir/'src-tauri/resources',lambda e,d:events.append((e,d)))
        app.store.set_setting('permissions',{'mode':'auto','tools':DEFAULT['tools']})
        app.store.set_setting('model_path',str(root_dir/'models/FrogNano-4B-2609-Q4_K_M.gguf'))
        chat=app.new_chat(str(project))
        try:
            started=time.monotonic()
            app.send(chat['id'],'Create a small complete offline portfolio website for an AI engineering graduate, with editable placeholders, HTML and separate CSS. Run it locally and give its localhost URL. Use short file chunks.',effort='think')
            deadline=time.monotonic()+180;seen=0
            while app.active and time.monotonic()<deadline:
                time.sleep(1)
                for event,data in events[seen:]:
                    if event=='progress':print(data.get('label'),flush=True)
                seen=len(events)
            result=app.chat(chat['id']);errors=[d.get('message') for e,d in events if e=='error'];serves=False
            if app.previews.servers:
                server=next(iter(app.previews.servers.values()))
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/') as r:
                    serves=r.status==200 and b'<html' in r.read().lower()
            report={'completed':not app.active,'errors':errors,'applied_edits':len([a for a in result['actions'] if a['kind']=='edit' and a['status']=='applied']),
                    'files':[p.name for p in project.iterdir()],'previews':len(app.previews.servers),'http_preview_passed':serves,
                    'seconds':round(time.monotonic()-started,1),'response_tail':result['messages'][-1]['content'][-800:] if result['messages'] else ''}
            report['passed']=report['completed'] and not errors and serves and report['applied_edits']>=2
            print(json.dumps(report,indent=2));(root_dir/'reports/local-website-live.json').write_text(json.dumps(report,indent=2))
        finally:app.close()
        if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
