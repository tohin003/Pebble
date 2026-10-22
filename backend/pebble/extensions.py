"""Reviewed skills, declarative hooks and allowlisted MCP HTTP plugins."""

import json
import hashlib
import re
import threading
import time
import uuid
from jsonschema import Draft202012Validator
from .policy import PolicyError
from .mcp_transport import MCP, MCPNotDispatched, MCPUncertain, endpoint, stdio_config, valid_arguments, valid_schema
from .skill_bundle import make_bundle, import_bundle, verify, load, packed
from . import connector_effects, ui_guidance


class Extensions:
    def __init__(self, store):
        self.store, self.clients = store, {}
        self.client_lock = threading.RLock()
        store.query("CREATE TABLE IF NOT EXISTS extension_tool_routes(run_id TEXT,plugin_id TEXT,tool TEXT,digest TEXT,snapshot TEXT,PRIMARY KEY(run_id,plugin_id,tool))")
        store.query(
            "CREATE TABLE IF NOT EXISTS extensions(id TEXT PRIMARY KEY,kind TEXT,name TEXT,content TEXT,config TEXT,enabled INTEGER,created REAL)"
        )
        store.query('CREATE TABLE IF NOT EXISTS extension_bundles(id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
        from .hooks import HookEngine
        self.hooks = HookEngine(store)
        from .mcp_content import Content
        self.content = Content(self)
        for item in self.list():
            if item.get('error'):
                # F7-1: disabled and reported by list(); never rebuilt from its stored content.
                store.query('UPDATE extensions SET enabled=0 WHERE id=?', (item['id'],))
                continue
            if item['kind'] == 'plugin' and item['enabled'] and not item['config'].get('catalogue_digest'):
                store.query('UPDATE extensions SET enabled=0 WHERE id=?', (item['id'],))
            if item['kind'] == 'skill' and not item['config'].get('bundle_digest'):
                try:
                    self._save_bundle(item['id'], make_bundle(item['content'], item['name']))
                except Exception:  # X20 F20-06: yaml.YAMLError and any unreadable legacy row are disabled, never fatal
                    store.query("UPDATE extensions SET enabled=0,config=? WHERE id=?", (json.dumps({'migration_error': 'invalid_legacy_skill'}), item['id']))
            elif item['kind'] == 'skill' and item['enabled']:
                try:
                    self.bundle(item['id'])
                except Exception:  # X20 F20-06: PolicyError, and AttributeError/TypeError from a non-object or BLOB snapshot
                    # F7-2: a stored snapshot that no longer verifies is disabled and reported at startup.
                    store.query('UPDATE extensions SET enabled=0,config=? WHERE id=?',
                                (json.dumps({**item['config'], 'startup_error': 'snapshot_unverified'}), item['id']))

    def list(self):
        rows = self.store.query("SELECT * FROM extensions ORDER BY created")
        for x in rows:
            x["enabled"] = bool(x["enabled"])
            try:
                x["config"] = json.loads(x["config"])
                if not isinstance(x["config"], dict):
                    raise ValueError('not an object')
            except (TypeError, ValueError):
                # F7-1: an unreadable stored configuration disables this one extension instead of stopping
                # startup. The stored bytes stay untouched as evidence; the person removes and reinstalls it.
                x["config"], x["enabled"] = {}, False
                x["error"] = 'Its stored configuration is unreadable, so it was disabled. Remove it and install it again.'
            x["capabilities"] = (
                x["config"].get("tools", [])
                if x["kind"] == "plugin"
                else ["task guidance"]
                if x["kind"] == "skill"
                else ["isolated Python decision worker · no project/network access"] if x['config'].get('worker')
                else ["deny / request review"]
            )
        return rows

    def get(self, ident, enabled=False):
        item = next(
            (
                x
                for x in self.list()
                if x["id"] == ident and (x["enabled"] or not enabled)
            ),
            None,
        )
        if not item:
            raise PolicyError("This extension is missing or disabled.")
        return item

    def add(self, kind, name, content, url=None):
        self.boundary()
        if (
            kind not in {"skill", "hook", "plugin"}
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 80
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 64000
        ):
            raise ValueError("Enter an extension name and content under 64 KB.")
        if len(self.list()) >= 50:
            raise ValueError("Remove an extension before adding more than 50.")
        config = {}
        if kind == "hook":
            from .hooks import validate_config
            config = validate_config(json.loads(content))
        if kind == "plugin":
            config = json.loads(content)
            transport = config.get('transport', 'http') if isinstance(config, dict) else None
            if (not isinstance(config, dict) or transport not in {'http', 'stdio'}
                    or not isinstance(config.get('tools',[]), list) or len(config.get('tools',[])) > 8
                    or any(not isinstance(n, str) or not re.fullmatch(r'[\w.-]{1,100}', n) for n in config.get('tools',[]))
                    or len(set(config.get('tools',[]))) != len(config.get('tools',[]))
                    or type(config.get('read_only', False)) is not bool):
                raise ValueError('Plugins require 1–8 distinct allowlisted tool names and an optional read_only boolean.')
            config.setdefault('tools',[])
            if config.get('protocol_era','legacy') not in {'legacy','modern'}:raise ValueError('Choose legacy or modern MCP protocol mode.')
            from .mcp_content import identifier
            for key,data_kind in [('resources','resource'),('prompts','prompt')]:
                values=config.get(key,[])
                if not isinstance(values,list) or len(values)>8:raise ValueError('Allowlist at most eight resources or prompts.')
                for value in values:identifier(data_kind,value)
                if len(set(values))!=len(values):raise ValueError('Duplicate resource/prompt allowlist.')
            if not any(config.get(k) for k in ('tools','resources','prompts')):raise ValueError('Allowlist a tool, resource or prompt.')
            if 'effects' in config:
                # The person's effect classes; checked against the server schemas at review.
                if not isinstance(config['effects'], dict):raise ValueError('Effect classes map allowlisted tools to their reviewed class.')
                connector_effects.check_effects(config['effects'], config['tools'])
            if transport == 'stdio':
                config = stdio_config(config)
            else:
                if set(config) - {'tools', 'resources', 'prompts', 'protocol_era', 'read_only', 'transport', 'effects'}:
                    raise ValueError('HTTP plugin configuration contains unsupported fields.')
                endpoint(url)
                config.update(url=url, transport='http')
        ident = uuid.uuid4().hex
        self.store.query(
            "INSERT INTO extensions VALUES(?,?,?,?,?,?,?)",
            (ident, kind, name.strip(), content, json.dumps(config), 0, time.time()),
        )
        if kind == 'skill':
            try:
                self._save_bundle(ident, make_bundle(content, name))
            except Exception:
                self.remove(ident)
                raise
        return ident

    def _save_bundle(self, ident, bundle):
        verify(bundle)
        config = {k: bundle[k] for k in ('version', 'digest', 'main', 'metadata', 'files')}
        config['bundle_digest'] = bundle['digest']
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Install bundles outside an application transaction.')
            with db:
                db.execute('INSERT OR REPLACE INTO extension_bundles VALUES(?,?)', (ident, packed(bundle)))
                db.execute('UPDATE extensions SET config=?,name=? WHERE id=?', (packed(config), bundle['metadata']['name'], ident))

    def boundary(self):
        with self.store.lock:
            if self.store.db.in_transaction:
                raise PolicyError('Extension operations require an outside transaction boundary.')

    def bundle(self, ident, digest=None):
        self.boundary()
        item = self.get(ident)
        rows = self.store.query('SELECT snapshot FROM extension_bundles WHERE id=?', (ident,))
        if not rows:
            raise PolicyError('No reviewed skill snapshot is available.')
        snapshot = rows[0]['snapshot']
        if not isinstance(snapshot, str):  # X20 F20-06: a BLOB or other non-text snapshot is refused, not a crash
            raise PolicyError('Skill snapshot changed or is invalid; install a reviewed version.')
        if len(snapshot.encode()) > 800_000:
            raise PolicyError('Stored skill snapshot exceeded its validation budget.')
        bundle = json.loads(snapshot)
        if not isinstance(bundle, dict):
            raise PolicyError('Skill snapshot changed or is invalid; install a reviewed version.')
        verify(bundle, digest or item['config'].get('bundle_digest'))
        if item['content'] != load(bundle)['untrusted_skill']:
            raise PolicyError('Skill instructions changed after review.')
        return bundle

    def import_skill(self, path):
        self.boundary()
        bundle = import_bundle(path)
        # An inline installation cannot validate required external references.
        # Install the complete immutable snapshot in one transaction instead.
        if len(self.list()) >= 50:
            raise ValueError('Remove an extension before adding more than 50.')
        ident = uuid.uuid4().hex
        content = load(bundle)['untrusted_skill']
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Install bundles outside an application transaction.')
            with db:
                config = {k: bundle[k] for k in ('version', 'digest', 'main', 'metadata', 'files')}
                config['bundle_digest'] = bundle['digest']
                db.execute('INSERT INTO extensions VALUES(?,?,?,?,?,?,?)', (ident, 'skill', bundle['metadata']['name'], content, packed(config), 0, time.time()))
                db.execute('INSERT INTO extension_bundles VALUES(?,?)', (ident, packed(bundle)))

        return ident

    def toggle(self, id, enabled):
        self.boundary()
        if type(enabled) is not bool:
            raise ValueError("Enabled must be a boolean.")
        item = self.get(id)
        if enabled and item.get('error'):
            raise PolicyError(item['error'])  # F7-1: an unreadable configuration is never re-enabled
        if enabled and item['kind'] == 'skill':
            self.bundle(id)
        if enabled and item['kind'] == 'hook':
            from .hooks import validate_config, supported
            checked = validate_config(item['config'])
            if checked.get('worker') and not supported():
                raise PolicyError('Isolated executable hooks currently require verified macOS confinement.')
        if enabled and item['kind'] == 'plugin':
            client = MCP(config=item['config'],auth=getattr(self,'auth',None),plugin_id=id)
            try:
                tools = self.reviewed_tools(item, client.tools()) if item['config']['tools'] else []
                if not client.initialized:client.initialize()
                config = {**item['config'], 'schemas': tools, 'protocol': client.protocol}
                if 'effects' in item['config']:
                    config['effects'] = connector_effects.review_effects(tools, item['config']['effects'])
                if item['config'].get('resources') or item['config'].get('prompts'):
                    config['content_catalogue']=self.content.review(config,client)
                    config['content_digest']=self.content.digest(config)
                config['catalogue_digest'] = self.catalogue_digest(config)
                self.store.query('UPDATE extensions SET config=? WHERE id=?', (json.dumps(config), id))
            finally:
                client.close()
        self.store.query(
            "UPDATE extensions SET enabled=? WHERE id=?", (int(enabled), id)
        )
        if not enabled:
            self.close_plugin(id)
        return True

    @staticmethod
    def local_refs(value, depth=0):
        if depth > 32:
            raise ValueError("Plugin schema is too deeply nested.")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("Remote schema references are blocked.")
                Extensions.local_refs(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                Extensions.local_refs(child, depth + 1)

    def remove(self, id):
        self.boundary()
        self.store.query("DELETE FROM extensions WHERE id=?", (id,))
        self.store.query('DELETE FROM extension_bundles WHERE id=?', (id,))
        self.close_plugin(id)
        return True

    def skills(self, text, guidance=False):
        words = set(re.findall(r"\w{3,}", text.lower()))
        skills = [x for x in self.list() if x["enabled"] and x["kind"] == "skill" and x['id'] != ui_guidance.ID]
        scored = sorted(
            skills,
            key=lambda x: len(
                words
                & set(
                    re.findall(
                        r"\w{3,}", (x["name"] + " " + x['config'].get('metadata', {}).get('description', '')).lower()
                    )
                )
            ),
            reverse=True,
        )
        matches = [
            x
            for x in scored
            if words
            & set(re.findall(r"\w{3,}", (x["name"] + " " + x['config'].get('metadata', {}).get('description', '')).lower()))
        ][:2]
        menu = [
            self.skill_metadata(x)
            for x in matches
        ]
        # Pebble's own UI guidance joins only when the runner offers it. A failed pin is not offered.
        if guidance is True:
            try:
                menu.append(ui_guidance.metadata())
            except PolicyError:
                pass
        return menu

    @staticmethod
    def skill_metadata(item):
        config = item['config']
        return {'id': item['id'], 'name': item['name'], 'digest': config.get('bundle_digest'),
            'description': config.get('metadata', {}).get('description', item['name']),
            'files': len(config.get('files', [])), 'authorization': 'none'}

    def select_skill(self, name):
        # The reserved ID always names Pebble's UI guidance; its name is one candidate among installed names.
        if name == ui_guidance.ID:
            return ui_guidance.metadata()
        matches = [x for x in self.list() if x['kind'] == 'skill' and x['enabled'] and x['id'] != ui_guidance.ID and (x['id'] == name or x['name'].casefold() == name.casefold())]
        builtin = name.casefold() == ui_guidance.NAME.casefold()
        if len(matches) + builtin != 1:
            raise PolicyError('Choose one enabled skill by its exact name or ID; use /skills.')
        if builtin:
            return ui_guidance.metadata()
        self.bundle(matches[0]['id'])
        return self.skill_metadata(matches[0])

    def load_skill(self, id, digest=None, reference=None, offset=0, limit=3000):
        if id == ui_guidance.ID:
            return {'id': id, **load(ui_guidance.bundle(), digest, reference, offset, limit)}
        item = self.get(id, enabled=True)
        if item["kind"] != "skill":
            raise PolicyError("Choose an enabled skill.")
        return {'id': id, **load(self.bundle(id, digest), digest, reference, offset, limit)}

    def inspect_skill(self, id, digest=None, reference=None, offset=0, limit=3000):
        if id == ui_guidance.ID:
            return {'id': id, **load(ui_guidance.bundle(), digest, reference, offset, limit)}
        item = self.get(id)
        if item['kind'] != 'skill':
            raise PolicyError('Choose an installed skill snapshot.')
        return {'id': id, **load(self.bundle(id, digest), digest, reference, offset, limit)}

    def hook(self, event, text='', **context):
        result = self.hooks.dispatch(self.list(), event, text, **context)
        if result['decision'] == 'deny':
            raise PolicyError('Blocked by hook: ' + result['blocked_by'])
        return result['decision']

    def plugin_menu(self):
        return [
            {
                "id": x["id"],
                "name": x["name"],
                "tools": [
                    {
                        "name": t["name"],
                        "description": t.get("description", "")[:150],
                        "input_schema": t.get("inputSchema", {}),
                    }
                    for t in x["config"].get("schemas", [])
                ],
            }
            for x in self.list()
            if x["enabled"] and x["kind"] == "plugin"
        ]

    @staticmethod
    def catalogue_digest(config):
        value={k:config.get(k) for k in ('transport','url','command','args','read_files','launch_files','protocol','tools','schemas')}
        if config.get('auth_binding'):value['auth_binding']=config['auth_binding']
        for key in ('resources','prompts','content_catalogue','content_digest','protocol_era','effects'):
            if key in config:value[key]=config[key]
        return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()

    def reviewed_tools(self, item, specs):
        names = [s.get('name') for s in specs]
        if any(not isinstance(n, str) or not re.fullmatch(r'[\w.-]{1,100}', n) for n in names) or len(set(names)) != len(names):
            raise PolicyError('MCP returned invalid or duplicate tool names.')
        selected = []
        for name in item['config']['tools']:
            if name not in names:
                raise PolicyError('An allowlisted tool is missing from the MCP server.')
            spec = specs[names.index(name)]
            if len(json.dumps(spec, allow_nan=False).encode()) > 20000 or not isinstance(spec.get('description', ''), str):
                raise PolicyError('MCP tool metadata exceeds its review budget.')
            for key in ('inputSchema', 'outputSchema'):
                if key == 'outputSchema' and key not in spec:
                    continue
                schema = spec.get(key)
                if not isinstance(schema,(dict,bool)):
                    raise PolicyError('MCP schemas must be an object or boolean.')
                self.local_refs(schema)
                self.safe_schema(schema)
                if not valid_schema(schema):raise PolicyError('Invalid or unsupported MCP schema.')
                if item['config'].get('protocol_era')=='modern' and key=='inputSchema':
                    from .mcp_schema import header_routes
                    header_routes(schema)
            # Preserve all metadata in the review digest. Annotations are never authority.
            selected.append(spec)
        return selected

    @staticmethod
    def safe_schema(value, depth=0, count=None):
        from .mcp_schema import inspect
        inspect(value)

    def plugin_index(self):
        return [{'plugin_id': x['id'], 'name': x['name'], 'tool_count': len(x['config']['tools']),
            'transport': x['config'].get('transport', 'http'), 'digest': x['config'].get('catalogue_digest'),
            'content_count':len(x['config'].get('resources',[]))+len(x['config'].get('prompts',[])), 'authorization': 'none'}
            for x in self.list() if x['kind'] == 'plugin' and x['enabled']]

    def search_plugin_tools(self, query, limit=4):
        if not isinstance(query, str) or not 1 <= len(query) <= 200 or type(limit) is not int or not 1 <= limit <= 8:
            raise PolicyError('Use a plugin search of 1–200 characters and limit 1–8.')
        words = set(re.findall(r'\w+', query.casefold()))
        matches = []
        for item in self.list():
            if item['kind'] != 'plugin' or not item['enabled']:
                continue
            for spec in item['config'].get('schemas', []):
                score = len(words & set(re.findall(r'\w+', (item['name']+' '+spec['name']+' '+spec.get('description', '')).casefold())))
                if score:
                    matches.append((score, {'plugin_id': item['id'], 'plugin': item['name'], 'tool': spec['name'],
                        'description': spec.get('description', '')[:240], 'digest': item['config'].get('catalogue_digest'), 'authorization': 'none'}))
        matches.sort(key=lambda pair: (-pair[0], pair[1]['plugin_id'], pair[1]['tool']))
        return {'origin': 'reviewed_plugin_metadata', 'untrusted_matches': [x[1] for x in matches[:limit]], 'total_matches': len(matches), 'omitted': max(0, len(matches)-limit), 'authorization': 'none'}

    def checked_plugin(self, id, digest=None):
        if getattr(self,'packages',None):self.packages.validate(id)
        item = self.get(id, enabled=True)
        config = item['config']
        if item['kind'] != 'plugin' or not config.get('catalogue_digest') or self.catalogue_digest(config) != config['catalogue_digest'] or digest is not None and digest != config['catalogue_digest']:
            raise PolicyError('MCP configuration changed or needs a fresh native review.')
        return item

    def load_plugin_tool(self, id, tool, digest, run_id):
        self.boundary()
        if not run_id:
            raise PolicyError('Plugin schema loading requires an owning run.')
        item = self.checked_plugin(id, digest)
        rows = self.store.query('SELECT plugin_id,tool FROM extension_tool_routes WHERE run_id=?', (run_id,))
        if len(rows) >= 4 and not any(r['plugin_id'] == id and r['tool'] == tool for r in rows):
            raise PolicyError('A run loads at most four complete plugin schemas.')
        spec = next((s for s in item['config']['schemas'] if s['name'] == tool), None)
        if spec is None:
            raise PolicyError('This MCP tool is not allowlisted.')
        snapshot = {'plugin_id': id, 'tool': tool, 'digest': digest, 'protocol': item['config']['protocol'], 'untrusted_schema': spec, 'authorization': 'none'}
        self.store.query('INSERT OR REPLACE INTO extension_tool_routes VALUES(?,?,?,?,?)', (run_id, id, tool, digest, json.dumps(snapshot)))
        return snapshot

    def loaded_plugin_tools(self, run_id):
        rows = self.store.query('SELECT * FROM extension_tool_routes WHERE run_id=? ORDER BY plugin_id,tool', (run_id,))
        output = []
        for row in rows:
            item = self.checked_plugin(row['plugin_id'], row['digest'])
            spec = next((s for s in item['config']['schemas'] if s['name'] == row['tool']), None)
            snapshot = json.loads(row['snapshot'])
            if snapshot != {'plugin_id': row['plugin_id'], 'tool': row['tool'], 'digest': row['digest'], 'protocol': item['config']['protocol'], 'untrusted_schema': spec, 'authorization': 'none'}:
                raise PolicyError('Stored plugin route changed after schema loading.')
            output.append(snapshot)
        return output

    def plugin_payload(self, id, tool, arguments, run_id=None, digest=None):
        item = self.checked_plugin(id, digest)
        if tool not in item['config']['tools']:
            raise PolicyError('This plugin tool has not been granted access.')
        if run_id:
            rows = self.store.query('SELECT digest FROM extension_tool_routes WHERE run_id=? AND plugin_id=? AND tool=?', (run_id, id, tool))
            if not rows or rows[0]['digest'] != item['config']['catalogue_digest'] or digest != rows[0]['digest']:
                raise PolicyError('Load this plugin schema in the current run before proposing a call.')
        if not isinstance(arguments, str) or len(arguments.encode()) > 12000:
            raise PolicyError('Plugin arguments must be a bounded JSON object.')
        try:
            args = json.loads(arguments, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            spec = next(s for s in item['config']['schemas'] if s['name'] == tool)
            if not isinstance(args, dict) or not valid_arguments(spec['inputSchema'], args):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise PolicyError('Plugin arguments do not match the reviewed tool schema.') from None
        return {'plugin_id': id, 'name': item['name'], 'tool': tool, 'arguments': args,
            'url': item['config'].get('url'), 'transport': item['config'].get('transport', 'http'),
            'digest': item['config']['catalogue_digest'], 'run_id': run_id, 'requires_review': True}

    def classify_payload(self, payload, run_id=None, human_texts=None, untrusted_texts=None):
        """Proposal-time effect classification (host-side; PolicyError when it must not be proposed).

        Human and untrusted text default to the run's host-recorded origins.
        """
        effects = getattr(self, 'effects', None)
        if effects is None:
            raise PolicyError('Connector effect review is unavailable.')
        item = self.checked_plugin(payload['plugin_id'], payload['digest'])
        if human_texts is None or untrusted_texts is None:
            human, untrusted = connector_effects.run_texts(self.store, run_id or payload.get('run_id'))
            human_texts = human if human_texts is None else human_texts
            untrusted_texts = untrusted if untrusted_texts is None else untrusted_texts
        exposure = None
        if getattr(self, 'auth', None) is not None:
            try:exposure = self.auth.metadata(payload['plugin_id']).get('credential_exposure')
            except Exception:exposure = None
        return effects.classify(item, payload, human_texts, untrusted_texts, exposure)

    def plugin_call(self, payload, run_id=None, cancel=None, action_id=None):
        if payload.get('run_id') != run_id:
            raise PolicyError('This MCP route belongs to another run.')
        checked = self.plugin_payload(payload['plugin_id'], payload['tool'], json.dumps(payload['arguments']), run_id, payload.get('digest'))
        if any(checked.get(k) != payload.get(k) for k in ('url', 'transport', 'digest')):
            raise PolicyError('Plugin route changed after native review.')
        item = self.checked_plugin(payload['plugin_id'], payload['digest'])
        effect, ledger, arguments, injected = payload.get('effect'), getattr(self, 'effects', None), checked['arguments'], None
        if effect is None and item['config'].get('effects') is not None:
            raise PolicyError('This connector has reviewed effect classes; the host must classify the call before review.')
        if effect is not None:
            # Classified effects are journaled before dispatch, so they need the ledger and their broker action.
            if ledger is None or action_id is None:
                raise PolicyError('A classified connector effect needs the host effect ledger and its broker action.')
            ledger.check(item, payload, action_id)
            injected = connector_effects.inject_idempotency(item['config'], checked['tool'], checked['arguments'], action_id)
            arguments = injected['arguments']
            spec = next(s for s in item['config']['schemas'] if s['name'] == checked['tool'])
            if arguments is not checked['arguments'] and not valid_arguments(spec['inputSchema'], arguments):
                raise PolicyError('The host idempotency key does not fit the reviewed tool schema.')
        owner = (run_id or 'native-review', payload['plugin_id'])
        with self.client_lock:
            client = self.clients.get(owner)
            if client is None:
                client = MCP(config=item['config'], cancel=cancel,auth=getattr(self,'auth',None),plugin_id=payload['plugin_id'])
                self.clients[owner] = client
        try:
            # A server catalogue is checked on the same session immediately before every call.
            schemas = self.reviewed_tools(item, client.tools())
            observed = {**item['config'], 'schemas': schemas, 'protocol': client.protocol}
            if self.catalogue_digest(observed) != payload['digest']:
                raise PolicyError('MCP tool schema/protocol drifted; disable and review the plugin again before effects.')
            if effect is not None:
                ledger.admit(action_id, payload, item, run_id, injected)
            try:
                result=client.call(checked['tool'],arguments,capture=lambda raw:self.content.capture(raw,'tool',checked['tool'],payload,run_id,cancel,client.modern))
                spec=next(s for s in schemas if s['name']==checked['tool'])
                if 'outputSchema' in spec and not result['is_error']:
                    try:
                        if not result.get('has_structured') or not valid_arguments(spec['outputSchema'],result['untrusted_structured']):raise ValueError()
                    except Exception:raise MCPUncertain('MCP output did not match its reviewed output schema; reconcile the executed effect.') from None
            except BaseException as exc:
                dispatched = client.call_dispatched and not isinstance(exc, MCPNotDispatched)
                if effect is not None:
                    try:ledger.settle(action_id, 'uncertain' if dispatched else 'not_dispatched')
                    except Exception:pass
                if dispatched and isinstance(exc, PolicyError):
                    # A refusal after dispatch is not proof that nothing happened.
                    raise MCPUncertain('MCP call failed after dispatch; inspect its remote outcome.') from None
                raise
            receipt = {**result, 'route_digest': payload['digest'], 'protocol': client.protocol}
            if effect is not None:
                ledger.settle(action_id, 'responded', is_error=result['is_error'],
                              remote_id=connector_effects.result_remote_id(item['config'], checked['tool'], result))
                receipt.update(effect_class=effect['class'], effect_assigned=effect['assigned'],
                               idempotency=injected['idempotency'], recipient_count=effect['recipient_count'])
            return receipt
        except Exception:
            self.close_run(run_id or 'native-review')
            raise

    def readback_call(self, id, tool, arguments, digest, cancel=None):
        """Native readback: one reviewed read-class call on a fresh owned client, never an effect tool."""
        item = self.checked_plugin(id, digest)
        spec = next((s for s in item['config'].get('schemas', []) if s['name'] == tool), None)
        reviewed = (item['config'].get('effects') or {}).get(tool)
        if spec is None or tool not in item['config']['tools'] or not connector_effects.readback_eligible(spec, reviewed):
            raise PolicyError('Readback calls only a reviewed read-class tool.')
        if (not isinstance(arguments, dict) or len(json.dumps(arguments).encode()) > 12000
                or not valid_arguments(spec['inputSchema'], arguments)):
            raise PolicyError('Readback arguments do not match the reviewed read tool schema.')
        client = MCP(config=item['config'], cancel=cancel, auth=getattr(self,'auth',None), plugin_id=id,
                     timeout=connector_effects.READBACK_TIMEOUT)
        try:
            schemas = self.reviewed_tools(item, client.tools())
            if self.catalogue_digest({**item['config'], 'schemas': schemas, 'protocol': client.protocol}) != digest:
                raise PolicyError('MCP tool schema/protocol drifted; review the plugin again before readback.')
            result = client.call(tool, arguments)
            if 'outputSchema' in spec and not result['is_error'] and (not result.get('has_structured')
                    or not valid_arguments(spec['outputSchema'], result['untrusted_structured'])):
                raise PolicyError('Readback output did not match its reviewed output schema.')
            return result
        finally:
            client.close()

    def close_run(self, run_id):
        with self.client_lock:
            clients = [self.clients.pop(k) for k in list(self.clients) if k[0] == run_id]
        for client in clients:
            client.close()

    def close_plugin(self, id):
        with self.client_lock:
            clients = [self.clients.pop(k) for k in list(self.clients) if k[1] == id]
        for client in clients:
            client.close()

    def close(self):
        with self.client_lock:
            clients = list(self.clients.values()); self.clients.clear()
        for client in clients:
            client.close()
