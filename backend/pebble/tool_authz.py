"""Independent host authorization layers for one tool call (Phase 14, R05).

check() evaluates five layers separately. Each can refuse on its own and every
refusal names its layer; one failing layer never skips or satisfies another.

catalogue  The tool exists in the canonical host catalogue, the catalogue still
           has the digest the caller named, and the run pinned that digest. A
           changed catalogue refuses until the host explicitly re-pins the run.
schema     The arguments match the canonical spec exactly, never the model-visible
           copy: declared types and enums, string lengths, declared integer
           bounds (no implied 1-10000 range), required keys, no undeclared keys,
           no booleans for integers or integers for booleans, no NUL.
route      A plugin tool still names a loaded, reviewed route. The host supplies
           a resolver; this module never imports the extension registry.
actor      The host-supplied actor may call the tool. Models never call
           native-only routes; children call only their admitted set and never
           root-only tools; an unowned model call never gets root-only tools.
policy     The run's pinned policy_version against current settings. Narrowing
           applies immediately; broadening needs a re-pinned run.

Everything checked comes from host state passed in. Arguments, model output,
source text, peer messages and plugin metadata never name the actor, the
policy or a route. A Decision is receipt evidence, never an approval: the
permission broker, hooks, AgentGuard and native review still decide effects.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from .policy import PolicyError

LAYERS = ('catalogue', 'schema', 'route', 'actor', 'policy')
ACTORS = ('root', 'child', 'model', 'native')
MODES = ('ask', 'auto', 'full')                 # narrowest first
PIN_VERSION = 1
CHECK = 'tool_authz/1'
DEFAULT_MAX_STRING = 96_000                     # App.validate's existing ceiling, never raised here
INT_MIN, INT_MAX = -(1 << 63), (1 << 63) - 1    # representable range, not a policy default
MAX_TOOLS, MAX_ERRORS = 512, 16
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}')
FIELD = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
HEX64 = re.compile(r'[0-9a-f]{64}')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9:._-]{0,319}')

# Tools a child can never call, whatever its admitted set says: effects
# (runtime.EFFECTS except SpawnAgent, which children use within tree limits),
# root-run controls, human-facing inputs, process observation and plugin loads.
ROOT_ONLY = frozenset({
    'Write', 'Append', 'Edit', 'RunCommand', 'RunTests', 'RunRecipe', 'PluginCall', 'FetchPluginData', 'OpenApp',
    'StartPreview', 'UIRender', 'UIUpdate', 'RenderMod', 'IntegrateAgentPatch', 'ExecuteAgentEffect',
    'IntegrateWorktreeFile', 'StartCommand', 'StopProcess', 'ProposeGitChange', 'PrepareEnvironment',
    'SpawnWorktreeAgent', 'ReadProcess', 'WaitProcess', 'AskUserQuestion', 'UpdatePlan', 'MemoryPropose',
    'ReviewAgentResult', 'RetryAgent', 'PromoteAgentQuestion', 'ReadAgentPatch', 'ReadAgentMessage',
    'ReadAgentEffects', 'DismissAgentEffect', 'LoadPluginTool', 'LoadPluginData', 'RunPlan', 'WriteProcessInput',
    'ScreenObserve', 'ScreenAct', 'ResearchPlan', 'ResearchStatus', 'UpdateGoal',
    'RunCode'})  # Phase 37: a script calls tools as the root run; child agents are not offered it
# Trusted main-window routes. None is a model tool; a catalogue entry with one
# of these names (compared case-insensitively) is refused for every model actor.
NATIVE_ONLY = frozenset({
    'approve', 'reject', 'setting', 'question_answer', 'terminal_propose', 'process_dismiss',
    'extension_add', 'extension_import', 'extension_toggle', 'extension_remove', 'review_plugin_media',
    'agent_worktree_retire',
    # Phase 15 native memory and reviewed-handoff routes (defence in depth; W9 O4).
    'memory_contests', 'memory_resolve_contest', 'memory_relations', 'memory_forget_preview', 'memory_archive_export_to',
    'memory_archive_preview_file', 'memory_archive_import_file', 'model_handoff_preview', 'model_handoff_approve',
    'model_handoff_start',
    # Phase 16 native screen, browser and connector routes.
    'research_sources', 'screen_windows', 'screen_session_grant', 'screen_sessions', 'screen_handoff_done', 'screen_session_revoke',
    'browser_status', 'browser_start', 'browser_stop', 'browser_tabs', 'browser_open_tab', 'browser_profile_forget',
    # Phase 22 Pebble for Chrome: main window only.
    'browser_extension_status', 'browser_extension_pause', 'browser_extension_resume', 'browser_extension_stop',
    'browser_extension_unpair',
    # Phase 24 (V1-043) Connect Chrome: main window only.
    'browser_connect_status', 'browser_connect_install',
    'connector_send_confirm', 'connector_save_environment_route',
    # Phase 17 native notification, lifecycle, goal and export routes.
    'notification_status', 'notification_mute', 'lifecycle_event', 'lifecycle_quit_summary',
    'notification_native_pending', 'notification_native_delivered',
    'goal_list', 'goal_get', 'goal_create', 'goal_state', 'goal_criterion_add', 'goal_criterion_edit',
    'goal_criterion_check', 'goal_criterion_review', 'goal_step_add', 'goal_step_edit', 'goal_step_block',
    'goal_blockers', 'goal_continue',
    'export_preview', 'export_render', 'export_cancel', 'export_save', 'export_list', 'export_get', 'export_discard',
    'export_destination_bind', 'export_native_ready', 'export_pdf_attach', 'export_pdf_failed',
    # Phase 18 E06 native telemetry routes.
    'telemetry_get', 'telemetry_set', 'telemetry_export', 'telemetry_clear', 'telemetry_destination_bind',
    # Phase 19 native view routes (quiz attempts and Share, dashboard Refresh) and the artifact task-data routes.
    'artifact_submit', 'artifact_stop', 'artifact_refresh', 'quiz_attempt', 'quiz_start', 'quiz_save', 'quiz_submit',
    'quiz_share',
    # Phase 20 X10 native rewind routes: main window only; main.rs writes `surface` from the window label.
    'rewind_preview', 'rewind_apply',
    # Phase 20 C20-context (C09) context inspector and between-turns compaction: main window only, the same way.
    'context_inspect', 'context_compact',
    # Phase 22 s22-storage Data & storage routes: main window only (main.rs ALLOWED, never the pet).
    'storage_summary', 'storage_reveal', 'storage_delete', 'storage_set', 'storage_settings_resolve',
    'storage_restore_chat', 'storage_purge_chat', 'storage_refresh',
    # Phase 24 (t24-data): Recently deleted from the app, Delete all, forget confirmations, old sign-in copies.
    'storage_trash_chat', 'storage_trash_all', 'storage_purge_all', 'storage_forget_resolve', 'storage_cleanup_legacy',
    # Phase 24 (V1-125) the sidebar's Rename: main window only.
    'chat_rename',
    # Phase 24 (V1-029) held-task decisions: main window only; main.rs writes `surface`.
    'run_held_tests', 'run_held_finish', 'run_held_accept'})
# Plugin tools and their dynamic route: (target kind, must already be loaded in the run).
PLUGIN_ROUTES = {'PluginCall': ('tool', True), 'LoadPluginTool': ('tool', False),
                 'FetchPluginData': ('data', True), 'LoadPluginData': ('data', False)}
_PROPERTY_KEYS = {'string': {'type', 'enum', 'maxLength', 'minLength', 'description', 'title'},
                  'integer': {'type', 'enum', 'minimum', 'maximum', 'description', 'title'},
                  'boolean': {'type', 'enum', 'description', 'title'}}
_OBJECT_KEYS = {'type', 'properties', 'required', 'additionalProperties', 'description', 'title'}
_TYPES = {'string': str, 'integer': int, 'boolean': bool}


class _Refused(PolicyError):
    def __init__(self, reason, field=None):
        super().__init__(reason)
        self.field = field


@dataclass(frozen=True)
class Actor:
    """Who is calling, from host state only. Never construct one from tool arguments."""
    kind: str
    run_id: str = None
    agent_id: str = None
    generation: int = None

    @classmethod
    def root(cls, run_id):
        return cls('root', run_id)

    @classmethod
    def child(cls, run_id, agent_id, generation):
        return cls('child', run_id, agent_id, generation)

    @classmethod
    def model(cls):
        return cls('model')

    @classmethod
    def native(cls):
        return cls('native')

    def receipt(self):
        return {k: v for k, v in (('kind', self.kind), ('run_id', self.run_id), ('agent_id', self.agent_id),
                                  ('generation', self.generation)) if v is not None}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    tool: str
    refusal_layer: str
    reason: str
    field: str
    refusals: tuple                 # ((layer, reason, field), ...) in LAYERS order
    catalogue_digest: str           # current canonical digest, None when malformed
    policy_version: int             # current settings version evaluated, None when invalid
    pinned_policy_version: int
    effective_policy: dict          # meet(pinned, current) permissions, None when not established
    actor: dict
    route: dict

    def require(self):
        """This decision, or PolicyError with the first refusal's reason."""
        if not self.allowed:
            raise PolicyError(self.reason)
        return self

    def receipt(self):
        """Canonical, content-free facts to store with the tool receipt."""
        return {'check': CHECK, 'allowed': self.allowed, 'tool': self.tool, 'catalogue_digest': self.catalogue_digest,
                'policy_version': self.policy_version, 'pinned_policy_version': self.pinned_policy_version,
                'effective_mode': (self.effective_policy or {}).get('mode'), 'actor': self.actor,
                'route': self.route, 'refusal_layer': self.refusal_layer, 'field': self.field}


def _label(value):
    """A bounded printable name for messages; undeclared or odd names are JSON-quoted."""
    text = value if isinstance(value, str) else repr(value)
    return text if FIELD.fullmatch(text) else json.dumps(text[:64])


def _version(value):
    return type(value) is int and 0 <= value <= INT_MAX


def _index(tools):
    """(name -> entry, SHA-256 of the canonical entries), or PolicyError."""
    if not isinstance(tools, (list, tuple)) or not 1 <= len(tools) <= MAX_TOOLS:
        raise PolicyError('The canonical tool catalogue is malformed.')
    index = {}
    for entry in tools:
        fn = entry.get('function') if isinstance(entry, dict) else None
        name = fn.get('name') if isinstance(fn, dict) else None
        if (entry.get('type') if isinstance(entry, dict) else None) != 'function' or not isinstance(name, str) \
                or not NAME.fullmatch(name) or not isinstance(fn.get('parameters'), dict):
            raise PolicyError('The canonical tool catalogue has a malformed entry.')
        if name in index:
            raise PolicyError(f'The canonical tool catalogue lists {name} twice.')
        index[name] = entry
    try:
        text = json.dumps([index[k] for k in sorted(index)], sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)
        return index, hashlib.sha256(text.encode('utf-8')).hexdigest()
    except (TypeError, ValueError, RecursionError):
        raise PolicyError('The canonical tool catalogue is not canonical JSON.') from None


def catalogue_digest(tools):
    """SHA-256 hex of the canonical catalogue: entries sorted by name, canonical JSON."""
    return _index(tools)[1]


def canonical_catalogue():
    """The host's canonical schemas: the catalogue plus adapter schemas it does not list yet.

    Mirrors AgentManager.child_tools. The entries are the live host objects, so a
    copy mutated in place changes the digest instead of passing silently.
    """
    from .agent_effects import SCHEMAS as EFFECT_SCHEMAS
    from .child_worktrees import TOOLS as WORKTREE_SCHEMAS
    from .tool_catalogue import TOOLS
    listed = {t['function']['name'] for t in TOOLS}
    return list(TOOLS) + [t for t in EFFECT_SCHEMAS + WORKTREE_SCHEMAS if t['function']['name'] not in listed]


def default_groups():
    """Tool -> permission group, read from the host tables at call time."""
    from .child_worktrees import GROUP as WORKTREE_GROUP
    from .permissions import GROUP
    return {**WORKTREE_GROUP, **GROUP}


def pin(*, catalogue_digest, policy_version):
    """The authorization pin a new run stores as config['authz']."""
    if not isinstance(catalogue_digest, str) or not HEX64.fullmatch(catalogue_digest) or not _version(policy_version):
        raise PolicyError('A run pin needs a catalogue digest and a nonnegative policy version.')
    return {'version': PIN_VERSION, 'catalogue_digest': catalogue_digest, 'policy_version': policy_version}


def repin(previous, *, catalogue_digest, policy_version):
    """A replacement pin for an explicit host re-pin. The policy version never moves back.

    Re-pinning is a host decision (native review), never a tool effect. A re-pin
    that broadens permissions must also replace the run's config['permissions'].
    """
    if not isinstance(previous, dict) or previous.get('version') != PIN_VERSION \
            or not _version(previous.get('policy_version')) or not isinstance(previous.get('catalogue_digest'), str):
        raise PolicyError('The previous run pin is malformed.')
    if not _version(policy_version) or policy_version < previous['policy_version']:
        raise PolicyError('A re-pin cannot move the policy version backwards.')
    generation = previous.get('generation', 0)
    if not _version(generation):
        raise PolicyError('The previous run pin is malformed.')
    return {**pin(catalogue_digest=catalogue_digest, policy_version=policy_version), 'generation': generation + 1,
            'previous': {'catalogue_digest': previous['catalogue_digest'], 'policy_version': previous['policy_version']}}


def _spec_problem(spec):
    if not isinstance(spec, dict) or spec.get('type') != 'object' or set(spec) - _OBJECT_KEYS \
            or not isinstance(spec.get('properties'), dict) or spec.get('additionalProperties', False) is not False:
        return 'cannot be checked against a malformed canonical schema'
    required = spec.get('required', [])
    if not isinstance(required, list) or len(set(map(repr, required))) != len(required) \
            or any(not isinstance(k, str) or k not in spec['properties'] for k in required):
        return 'cannot be checked against a malformed canonical schema'
    for key, prop in spec['properties'].items():
        kind = prop.get('type') if isinstance(prop, dict) else None
        if not isinstance(key, str) or kind not in _PROPERTY_KEYS or set(prop) - _PROPERTY_KEYS[kind]:
            return 'cannot be checked against an unsupported canonical schema'
        bounds = [prop[k] for k in ('maxLength', 'minLength', 'minimum', 'maximum') if k in prop]
        if any(type(b) is not int for b in bounds) or any(prop.get(k, 0) < 0 for k in ('maxLength', 'minLength')) \
                or prop.get('minLength', 0) > prop.get('maxLength', DEFAULT_MAX_STRING) \
                or 'minimum' in prop and 'maximum' in prop and prop['minimum'] > prop['maximum']:
            return 'cannot be checked against a malformed canonical schema'
        if 'enum' in prop and (not isinstance(prop['enum'], list) or not prop['enum']
                               or any(type(v) is not _TYPES[kind] for v in prop['enum'])):
            return 'cannot be checked against a malformed canonical schema'
    return None


def _value_problem(prop, value):
    kind = prop['type']
    if kind == 'string':
        if type(value) is not str:
            return 'must be a string'
        limit = prop.get('maxLength', DEFAULT_MAX_STRING)
        if len(value) > limit:
            return f'exceeds maxLength {limit}' if 'maxLength' in prop else f'exceeds {limit} characters'
        if len(value) < prop.get('minLength', 0):
            return f'is shorter than minLength {prop["minLength"]}'
        if '\x00' in value:
            return 'must not contain NUL'
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            return 'must be valid Unicode text'
    elif kind == 'integer':
        if type(value) is not int:
            return 'must be an integer, not a boolean' if type(value) is bool else 'must be an integer'
        if 'minimum' in prop and value < prop['minimum']:
            return f'is below minimum {prop["minimum"]}'
        if 'maximum' in prop and value > prop['maximum']:
            return f'is above maximum {prop["maximum"]}'
        if not INT_MIN <= value <= INT_MAX:
            return 'is outside the 64-bit integer range'
    elif type(value) is not bool:
        return 'must be a boolean, not a number' if type(value) in (int, float) else 'must be a boolean'
    if 'enum' in prop and not any(type(v) is type(value) and v == value for v in prop['enum']):
        return ('must be one of: ' + ', '.join(map(str, prop['enum'])))[:300] + '. Use the required value on your next call'
    return None


def strict_schema_errors(spec, args):
    """[(field, problem), ...] for args against one canonical parameters schema; [] when valid.

    The field is the argument name (JSON-quoted when undeclared or unusual) or
    '$' for the whole object. At most MAX_ERRORS entries, deterministic order.
    """
    problem = _spec_problem(spec)
    if problem:
        return [('$', problem)]
    if type(args) is not dict:
        return [('$', 'must be a JSON object')]
    props = spec['properties']
    errors = [(_label(k), 'is not a declared argument') for k in sorted((k for k in args if k not in props), key=repr)]
    errors += [(k, 'is required') for k in spec.get('required', []) if k not in args]
    for key in sorted(k for k in args if k in props):
        problem = _value_problem(props[key], args[key])
        if problem:
            errors.append((key, problem))
    return errors[:MAX_ERRORS]


def _permissions(value, whose):
    if not isinstance(value, dict) or set(value) != {'mode', 'tools'} or value['mode'] not in MODES \
            or not isinstance(value['tools'], dict) or not value['tools'] \
            or any(not isinstance(k, str) or type(v) is not bool for k, v in value['tools'].items()):
        raise _Refused(f'The {whose} permissions are malformed; refusing.')
    return {'mode': value['mode'], 'tools': dict(value['tools'])}


def _meet(pinned, current):
    """Elementwise narrowest of two permission sets; a group missing from either is off."""
    groups = set(pinned['tools']) | set(current['tools'])
    return {'mode': MODES[min(MODES.index(pinned['mode']), MODES.index(current['mode']))],
            'tools': {g: pinned['tools'].get(g, False) and current['tools'].get(g, False) for g in sorted(groups)}}


def _catalogue_layer(name, tool, index, current, error, named, config):
    if index is None:
        raise _Refused(error)
    if name is None or name not in index:
        raise _Refused(f'Unknown tool {_label(tool)}; it is not in the canonical catalogue.')
    if named != current:
        raise _Refused('The canonical tool catalogue does not match the digest named for this check.')
    pinned = config.get('authz') if isinstance(config, dict) else None
    pinned = pinned.get('catalogue_digest') if isinstance(pinned, dict) else None
    if not isinstance(pinned, str) or not HEX64.fullmatch(pinned):
        raise _Refused('This run has no pinned tool catalogue; start a new run.')
    if pinned != current:
        raise _Refused('The tool catalogue changed after this run started; the run must be re-pinned before tools run.')


def _schema_layer(name, index, args):
    if index is None or name not in index:
        raise _Refused('Tool arguments cannot be checked without a canonical schema.', '$')
    errors = strict_schema_errors(index[name]['function']['parameters'], args)
    if errors:
        field, problem = errors[0]
        raise _Refused((f'Tool arguments {problem}.' if field == '$' else f'Tool argument {field} {problem}.'), field)


def _route_layer(name, args, resolve):
    if name not in PLUGIN_ROUTES:
        return None
    kind, loaded = PLUGIN_ROUTES[name]
    if not callable(resolve):
        raise _Refused('No plugin route resolver was supplied; the plugin route cannot be verified.')
    args = args if isinstance(args, dict) else {}
    request = {'via': name, 'plugin_id': args.get('plugin_id'), 'kind': 'tool' if kind == 'tool' else args.get('kind'),
               'target': args.get('tool' if kind == 'tool' else 'identifier'), 'digest': args.get('digest'),
               'requires_loaded': loaded}
    if any(not isinstance(request[k], str) or not 1 <= len(request[k]) <= 512 or '\x00' in request[k]
           for k in ('plugin_id', 'kind', 'target', 'digest')):
        raise _Refused('The plugin route identity is malformed.')
    resolved = resolve(dict(request))
    if not isinstance(resolved, dict) or resolved.get('reviewed') is not True \
            or any(type(resolved.get(k)) is not str or resolved.get(k) != request[k]
                   for k in ('plugin_id', 'kind', 'target', 'digest')) \
            or loaded and resolved.get('loaded') is not True:
        raise _Refused('This plugin route is not loaded and reviewed in this run, or it changed since review.')
    return {'plugin_id': request['plugin_id'], 'kind': request['kind'], 'target': request['target'],
            'digest': request['digest'], 'loaded': resolved.get('loaded') is True}


def _ident(value):
    return isinstance(value, str) and IDENT.fullmatch(value) is not None


def _actor_valid(actor):
    if actor.kind == 'child':
        return (_ident(actor.run_id) and _ident(actor.agent_id)
                and type(actor.generation) is int and 1 <= actor.generation <= INT_MAX)
    if actor.kind not in ACTORS or actor.agent_id is not None or actor.generation is not None:
        return False
    # A root is bound to its run; model and native callers may name one for receipts.
    return _ident(actor.run_id) if actor.kind == 'root' else actor.run_id is None or _ident(actor.run_id)


def _actor_layer(name, tool, actor, admitted, native_only):
    if type(actor) is not Actor:
        raise _Refused('The actor must come from host state, never from the request.')
    if not _actor_valid(actor):
        raise _Refused('The actor identity is malformed.')
    if actor.kind == 'native':
        return
    if name is not None and name.casefold() in {n.casefold() for n in native_only}:
        raise _Refused(f'{_label(tool)} is a native-only route; a model cannot call it.')
    if actor.kind == 'root':
        return
    if name in ROOT_ONLY:
        raise _Refused(f'{_label(tool)} requires the owning root run; a {actor.kind} cannot call it.')
    if actor.kind == 'model':
        return
    if not callable(admitted):
        raise _Refused('No admitted tool set was supplied for this child.')
    allowed = admitted(actor)
    if not isinstance(allowed, (set, frozenset, list, tuple)):
        raise _Refused("This child's admitted tool set is malformed.")
    if name not in allowed:
        raise _Refused(f"{_label(tool)} is outside this child's admitted tools.")


def _policy_layer(name, tool, config, version, policy, groups):
    pinned_pin = config.get('authz') if isinstance(config, dict) else None
    pinned_version = pinned_pin.get('policy_version') if isinstance(pinned_pin, dict) else None
    if not _version(pinned_version):
        raise _Refused('This run has no pinned policy version; start a new run.')
    if not _version(version):
        raise _Refused('The current policy version is invalid; refusing.')
    pinned = _permissions(config.get('permissions'), 'pinned')
    if version < pinned_version:
        raise _Refused('The policy version moved backwards since this run started; refusing.')
    if version == pinned_version and (policy is None or _permissions(policy, 'current') == pinned):
        current = effective = pinned
    else:
        # Content decides, whether or not the version moved (startup merges and
        # out-of-band writes do not bump it): narrowing applies at once and
        # broadening needs a re-pinned or new run.
        if policy is None:
            raise _Refused('Permissions changed after this run started; the current permissions are needed to apply them.')
        current = _permissions(policy, 'current')
        effective = _meet(pinned, current)
    group = groups.get(name) if isinstance(name, str) else None
    if group is not None and not effective['tools'].get(group, False):
        if pinned['tools'].get(group, False):
            raise _Refused(f'{_label(tool)} is disabled: permissions were narrowed during this run.')
        if current['tools'].get(group, False):
            raise _Refused(f'{_label(tool)} needs {group} permission granted after this run started; '
                           'a re-pinned or new run is required to use it.')
        raise _Refused(f'{_label(tool)} is disabled in Permissions.')
    return effective


def check(*, tool, args, actor, run_config, catalogue_digest, route=None, policy_version,
          catalogue=None, policy=None, admitted=None, groups=None, native_only=NATIVE_ONLY, layers=LAYERS):
    """Evaluate every layer independently and return a Decision. Never raises for bad input.

    tool            tool name requested
    args            parsed arguments (data only)
    actor           Actor built from host state
    run_config      the run's config: 'permissions' (pinned) and 'authz' (pin())
    catalogue_digest the current canonical digest the caller computed
    route           plugin route resolver: route(request) -> {'plugin_id', 'kind',
                    'target', 'digest', 'reviewed': True, 'loaded': bool} or None
    policy_version  current settings policy version
    catalogue       canonical schemas (default canonical_catalogue())
    policy          current permissions settings; needed once the version moved
    admitted        admitted(actor) -> collection of tool names, for children
    groups          tool -> permission group (default default_groups())
    native_only     names no model actor may call
    """
    refusals, outcome = [], {}
    name = tool if isinstance(tool, str) and NAME.fullmatch(tool) else None
    index = current = None
    error = 'The canonical tool catalogue is malformed.'
    try:
        index, current = _index(canonical_catalogue() if catalogue is None else catalogue)
    except PolicyError as exc:
        error = str(exc)
    except Exception as exc:
        error = f'The canonical tool catalogue could not be loaded ({type(exc).__name__}).'

    def run(layer, step):
        if layer not in layers:
            return
        try:
            outcome[layer] = step()
        except Exception as exc:
            reason = str(exc)[:300] if isinstance(exc, PolicyError) and str(exc) else \
                f'The {layer} check failed closed ({type(exc).__name__}).'
            refusals.append((layer, reason, getattr(exc, 'field', None)))

    run('catalogue', lambda: _catalogue_layer(name, tool, index, current, error, catalogue_digest, run_config))
    run('schema', lambda: _schema_layer(name, index, args))
    run('route', lambda: _route_layer(name, args, route))
    run('actor', lambda: _actor_layer(name, tool, actor, admitted, native_only))
    run('policy', lambda: _policy_layer(name, tool, run_config, policy_version, policy,
                                        default_groups() if groups is None else groups))
    first = refusals[0] if refusals else (None, None, None)
    pinned = run_config.get('authz') if isinstance(run_config, dict) else None
    pinned = pinned.get('policy_version') if isinstance(pinned, dict) else None
    return Decision(
        allowed=not refusals, tool=name, refusal_layer=first[0], reason=first[1], field=first[2],
        refusals=tuple(refusals), catalogue_digest=current,
        policy_version=policy_version if _version(policy_version) else None,
        pinned_policy_version=pinned if _version(pinned) else None,
        effective_policy=outcome.get('policy'),
        actor=actor.receipt() if type(actor) is Actor else None,
        route=outcome.get('route'))
