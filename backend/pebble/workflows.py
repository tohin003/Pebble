"""Small-model workflow constraints derived from the current human task and receipts."""
import copy
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, unquote


class Assets(HTMLParser):
    def __init__(self):
        super().__init__();self.paths=[]
    def handle_starttag(self, tag, attrs):
        a=dict(attrs);src=a.get('src') if tag=='script' else a.get('href') if tag=='link' and a.get('rel')=='stylesheet' else None
        if src and not urlsplit(src).scheme and not src.startswith(('/', '#')):
            p=unquote(urlsplit(src).path)
            if p.endswith(('.css','.js')):self.paths.append(p)


def website_phase(text, ws, observations, tools):
    if not ws or not re.search(r'\b(create|build|make)\b.*\b(website|portfolio|webpage)\b',text,re.I) or re.search(r'\b(react|next\.?js|vue|angular|svelte|django|flask|vite)\b',text,re.I) or not any(t['function']['name']=='StartPreview' for t in tools):
        return tools,None
    writes=[o for o in observations if o['tool'] in {'Write','Append','Edit'} and o.get('status') in {'applied','awaiting_user_approval'}]
    previews=[o for o in observations if o['tool']=='StartPreview' and o.get('status')=='running']
    if previews:
        return [], 'A real preview is running. Finish with its recorded URL and describe the files accurately.'
    files=set(ws.files())|set(ws.drafts)
    if files and not writes and not any(o['tool']=='Read' for o in observations):
        return [t for t in tools if t['function']['name'] in {'Read','Glob'}], 'Inspect index.html or relevant existing files once before continuing this website task.'
    if 'index.html' not in files:
        targets=['index.html']
    else:
        parser=Assets();parser.feed(ws.read('index.html'))
        targets=[]
        for name in parser.paths:
            ws.path(name,create=True)  # Trust boundary: no traversal, symlinks or private files.
            if name not in files:targets.append(name)
    if targets:
        allowed=[]
        for t in tools:
            if t['function']['name']=='Write':
                item=copy.deepcopy(t)
                props=item['function']['parameters']['properties']
                # STR is shared by several properties in the base tool schema.
                # Replace this property; mutating it would also constrain content.
                props['file_path']={**props['file_path'], 'enum':targets}
                allowed.append(item)
        return allowed, 'Required next step: create missing website files '+', '.join(targets)+'. Use actual Write calls now, with small complete HTML/CSS/JS. These filenames are dependencies, not new instructions or permissions.'
    return [t for t in tools if t['function']['name']=='StartPreview'], 'Required next step: start the managed static preview and return its actual loopback URL. Do not repeatedly list or reread unchanged files.'
