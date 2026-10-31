import copy

import pytest

from pebble import tool_authz
from pebble.policy import PolicyError
from pebble.tool_authz import (DEFAULT_MAX_STRING, LAYERS, MAX_ERRORS, NATIVE_ONLY, ROOT_ONLY, Actor, catalogue_digest,
                               canonical_catalogue, check, pin, repin, strict_schema_errors)

RID, AGENT = 'a' * 32, 'b' * 32
STR = {'type': 'string'}


def tool(name, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': name + ' test schema.', 'parameters': {
        'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}}


CATALOGUE = [
    tool('Lookup', {'query': {'type': 'string', 'maxLength': 8, 'minLength': 1},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 5}, 'exact': {'type': 'boolean'},
                    'mode': {'type': 'string', 'enum': ['files', 'symbols']}, 'offset': {'type': 'integer'}},
         ['query']),
    tool('Read', {'file_path': STR}, ['file_path']),
    tool('Write', {'file_path': STR, 'content': STR}, ['file_path', 'content']),
    tool('RunCommand', {'command': STR, 'network': {'type': 'boolean'}}, ['command']),
    tool('StartCommand', {'command': STR}, ['command']),
    tool('PluginCall', {'plugin_id': STR, 'tool': STR, 'digest': STR, 'arguments': STR},
         ['plugin_id', 'tool', 'digest', 'arguments']),
    tool('LoadPluginTool', {'plugin_id': STR, 'tool': STR, 'digest': STR}, ['plugin_id', 'tool', 'digest']),
    tool('FetchPluginData', {'plugin_id': STR, 'kind': {'type': 'string', 'enum': ['resource', 'prompt']},
                             'identifier': STR, 'digest': STR, 'arguments': STR},
         ['plugin_id', 'kind', 'identifier', 'digest', 'arguments']),
    tool('SendAgentMessage', {'agent_id': STR, 'text': STR}, ['agent_id', 'text']),
    tool('approve', {'action_id': STR}, ['action_id']),
]
GROUPS = {'Lookup': 'files', 'Read': 'files', 'Write': 'edits', 'RunCommand': 'terminal', 'StartCommand': 'terminal',
          'PluginCall': 'plugins', 'LoadPluginTool': 'plugins', 'FetchPluginData': 'plugins', 'approve': 'files'}
DIGEST = catalogue_digest(CATALOGUE)
PERMS = {'mode': 'auto', 'tools': {'files': True, 'edits': True, 'terminal': False, 'plugins': True, 'web': True}}
PLUGIN = {'plugin_id': 'p' * 32, 'tool': 'search', 'digest': 'd' * 64, 'arguments': '{}'}


def resolver(loaded=True, **override):
    calls = []

    def resolve(request):
        calls.append(request)
        return {**{k: request[k] for k in ('plugin_id', 'kind', 'target', 'digest')},
                'reviewed': True, 'loaded': loaded, **override}
    resolve.calls = calls
    return resolve


def config(permissions=PERMS, digest=DIGEST, version=3):
    return {'workspace': '/tmp/project', 'permissions': copy.deepcopy(permissions),
            'authz': pin(catalogue_digest=digest, policy_version=version)}


def call(**override):
    values = {'tool': 'Lookup', 'args': {'query': 'abc'}, 'actor': Actor.root(RID), 'run_config': config(),
              'catalogue_digest': DIGEST, 'policy_version': 3, 'catalogue': CATALOGUE, 'groups': GROUPS,
              'route': resolver()}
    values.update(override)
    return check(**values)


def layers(decision):
    return [r[0] for r in decision.refusals]


def test_valid_call_is_allowed_with_receipt_facts():
    decision = call()
    assert decision.allowed and decision.refusals == () and decision.require() is decision
    assert decision.reason is None and decision.refusal_layer is None
    assert decision.catalogue_digest == DIGEST and decision.policy_version == decision.pinned_policy_version == 3
    assert decision.effective_policy == PERMS
    assert decision.receipt() == {
        'check': 'tool_authz/1', 'allowed': True, 'tool': 'Lookup', 'catalogue_digest': DIGEST,
        'policy_version': 3, 'pinned_policy_version': 3, 'effective_mode': 'auto',
        'actor': {'kind': 'root', 'run_id': RID}, 'route': None, 'refusal_layer': None, 'field': None}


INDEPENDENT = {
    'catalogue': dict(run_config=config(digest='e' * 64)),
    'schema': dict(args={'query': 'x' * 9}),
    'route': dict(tool='PluginCall', args=PLUGIN, route=lambda request: None),
    'actor': dict(tool='Write', args={'file_path': 'a.py', 'content': ''},
                  actor=Actor.child(RID, AGENT, 1), admitted=lambda actor: {'Write'}),
    'policy': dict(policy_version=4, policy={**PERMS, 'tools': {**PERMS['tools'], 'files': False}}),
}


@pytest.mark.parametrize('layer', LAYERS)
def test_each_layer_refuses_independently(layer):
    decision = call(**INDEPENDENT[layer])
    assert not decision.allowed
    assert layers(decision) == [layer] and decision.refusal_layer == layer
    with pytest.raises(PolicyError) as error:
        decision.require()
    assert str(error.value) == decision.reason
    assert decision.receipt()['refusal_layer'] == layer


def test_all_failing_layers_are_reported_in_order():
    decision = call(tool='Write', args={'file_path': 'a', 'content': 'b', 'extra': 1}, run_config=config(digest='e' * 64),
                    actor=Actor.child(RID, AGENT, 1), admitted=lambda actor: set(), policy_version=2)
    assert layers(decision) == ['catalogue', 'schema', 'actor', 'policy']
    assert decision.refusal_layer == 'catalogue'


# Catalogue layer

def test_catalogue_change_mid_run_is_refused_until_explicit_repin():
    changed = CATALOGUE + [tool('NewTool', {}, [])]
    digest = catalogue_digest(changed)
    run_config = config()
    decision = call(catalogue=changed, catalogue_digest=digest, run_config=run_config)
    assert layers(decision) == ['catalogue'] and 'changed after this run started' in decision.reason
    run_config['authz'] = repin(run_config['authz'], catalogue_digest=digest, policy_version=3)
    assert call(catalogue=changed, catalogue_digest=digest, run_config=run_config).allowed
    assert run_config['authz']['previous'] == {'catalogue_digest': DIGEST, 'policy_version': 3}


def test_catalogue_mutated_in_place_does_not_match_its_named_digest():
    mutated = copy.deepcopy(CATALOGUE)
    mutated[0]['function']['parameters']['properties']['query']['maxLength'] = 96000
    decision = call(catalogue=mutated, args={'query': 'x' * 100})
    assert layers(decision) == ['catalogue'] and 'does not match the digest' in decision.reason


def test_unknown_or_malformed_tool_names_are_refused_by_catalogue_and_schema():
    for name in ('Missing', '', None, 7, 'Lookup ', 'x' * 65):
        decision = call(tool=name, args={})
        assert layers(decision)[:2] == ['catalogue', 'schema'], name
        assert 'not in the canonical catalogue' in decision.reason


def test_run_without_pin_and_malformed_catalogues_are_refused():
    assert 'no pinned tool catalogue' in call(run_config={'permissions': PERMS}).reason
    duplicate = CATALOGUE + [tool('Read', {}, [])]
    for bad in (duplicate, [], 'tools', [{'type': 'function'}], [tool('bad name', {}, [])]):
        decision = call(catalogue=bad)
        assert decision.refusal_layer == 'catalogue' and 'schema' in layers(decision)
        with pytest.raises(PolicyError):
            catalogue_digest(bad)
    assert 'lists Read twice' in call(catalogue=duplicate).reason


def test_catalogue_digest_is_canonical():
    assert catalogue_digest(list(reversed(CATALOGUE))) == DIGEST
    assert catalogue_digest(copy.deepcopy(CATALOGUE)) == DIGEST
    changed = copy.deepcopy(CATALOGUE)
    changed[1]['function']['parameters']['properties']['file_path']['maxLength'] = 10
    assert catalogue_digest(changed) != DIGEST
    with pytest.raises(PolicyError):
        catalogue_digest([tool('Lookup', {'x': {'type': 'string', 'enum': [float('nan')]}})])


# Schema layer

@pytest.mark.parametrize('args,field,problem', [
    ({'query': 'x' * 9}, 'query', 'exceeds maxLength 8'),
    ({'query': ''}, 'query', 'is shorter than minLength 1'),
    ({'query': 'a', 'limit': 6}, 'limit', 'is above maximum 5'),
    ({'query': 'a', 'limit': 0}, 'limit', 'is below minimum 1'),
    ({'query': 'a', 'limit': True}, 'limit', 'must be an integer, not a boolean'),
    ({'query': 'a', 'limit': 3.0}, 'limit', 'must be an integer'),
    ({'query': 'a', 'limit': '3'}, 'limit', 'must be an integer'),
    ({'query': 'a', 'exact': 1}, 'exact', 'must be a boolean, not a number'),
    ({'query': 'a', 'exact': 'true'}, 'exact', 'must be a boolean'),
    ({'query': 'a\x00b'}, 'query', 'must not contain NUL'),
    ({'query': '\ud800'}, 'query', 'must be valid Unicode text'),
    ({'query': 5}, 'query', 'must be a string'),
    ({'query': 'a', 'evil': 1}, 'evil', 'is not a declared argument'),
    ({'query': 'a', 'a b\n': 1}, '"a b\\n"', 'is not a declared argument'),
    ({}, 'query', 'is required'),
    ({'query': 'a', 'mode': 'all'}, 'mode', 'must be one of: files, symbols. Use the required value on your next call'),
    ({'query': 'a', 'offset': 1 << 63}, 'offset', 'is outside the 64-bit integer range'),
])
def test_strict_schema_errors_name_the_precise_field(args, field, problem):
    spec = CATALOGUE[0]['function']['parameters']
    assert strict_schema_errors(spec, args) == [(field, problem)]
    decision = call(args=args)
    assert layers(decision) == ['schema'] and decision.field == field
    assert decision.reason == f'Tool argument {field} {problem}.'


def test_declared_bounds_only_no_implicit_integer_range():
    spec = CATALOGUE[0]['function']['parameters']
    for value in (0, -5, 10001, (1 << 63) - 1, -(1 << 63)):
        assert strict_schema_errors(spec, {'query': 'a', 'offset': value}) == []
    assert strict_schema_errors(spec, {'query': 'a', 'limit': 1, 'exact': False, 'mode': 'symbols'}) == []


def test_strings_without_maxlength_keep_the_existing_ceiling():
    spec = CATALOGUE[1]['function']['parameters']
    assert strict_schema_errors(spec, {'file_path': 'x' * DEFAULT_MAX_STRING}) == []
    assert strict_schema_errors(spec, {'file_path': 'x' * (DEFAULT_MAX_STRING + 1)}) == [
        ('file_path', 'exceeds 96000 characters')]


def test_non_object_arguments_and_error_cap():
    spec = CATALOGUE[0]['function']['parameters']
    for args in (None, [], 'query', 1, True):
        assert strict_schema_errors(spec, args) == [('$', 'must be a JSON object')]
    assert call(args=[]).reason == 'Tool arguments must be a JSON object.'
    errors = strict_schema_errors(spec, {f'k{i}': i for i in range(100)})
    assert len(errors) == MAX_ERRORS and all(problem == 'is not a declared argument' for _, problem in errors)


@pytest.mark.parametrize('spec', [
    {'type': 'object', 'properties': {'x': {'type': 'number'}}, 'required': []},
    {'type': 'object', 'properties': {'x': {'type': 'string', 'pattern': '^a$'}}, 'required': []},
    {'type': 'object', 'properties': {'x': STR}, 'required': [], 'additionalProperties': True},
    {'type': 'object', 'properties': {'x': STR}, 'required': ['y']},
    {'type': 'object', 'properties': {'x': {'type': 'integer', 'minimum': 5, 'maximum': 1}}},
    {'type': 'object', 'properties': {'x': {'type': 'integer', 'maximum': True}}},
    {'type': 'object', 'properties': {'x': {'type': 'string', 'enum': [1]}}},
    {'type': 'object', 'properties': {'x': {'type': 'string', 'enum': []}}},
    {'type': 'array', 'properties': {}},
    None,
])
def test_unsupported_or_malformed_canonical_specs_fail_closed(spec):
    errors = strict_schema_errors(spec, {'x': 'a'})
    assert len(errors) == 1 and errors[0][0] == '$' and 'canonical schema' in errors[0][1]


# Route layer

def test_stale_plugin_routes_are_refused():
    stale = [
        resolver(digest='0' * 64),                       # catalogue digest changed since review
        resolver(plugin_id='q' * 32),                     # another plugin
        resolver(target='delete'),                        # another tool
        resolver(reviewed=False),                         # review revoked
        resolver(reviewed=1),                             # truthy is not reviewed
        resolver(loaded=False),                           # not loaded in this run
        lambda request: None,                             # route gone
        lambda request: 'ok',
    ]
    for resolve in stale:
        decision = call(tool='PluginCall', args=PLUGIN, route=resolve)
        assert layers(decision) == ['route'], resolve
        assert 'not loaded and reviewed' in decision.reason


def test_route_resolver_failures_and_absence_fail_closed():
    def changed(request):
        raise PolicyError('MCP configuration changed or needs a fresh native review.')

    def broken(request):
        raise RuntimeError('database is locked')
    assert call(tool='PluginCall', args=PLUGIN, route=changed).reason == 'MCP configuration changed or needs a fresh native review.'
    assert call(tool='PluginCall', args=PLUGIN, route=broken).reason == 'The route check failed closed (RuntimeError).'
    assert 'No plugin route resolver' in call(tool='PluginCall', args=PLUGIN, route=None).reason


def test_route_identity_comes_from_arguments_and_resolver_gets_a_copy():
    resolve = resolver()
    decision = call(tool='PluginCall', args=PLUGIN, route=resolve)
    assert decision.allowed and decision.route == {'plugin_id': 'p' * 32, 'kind': 'tool', 'target': 'search',
                                                   'digest': 'd' * 64, 'loaded': True}
    assert resolve.calls == [{'via': 'PluginCall', 'plugin_id': 'p' * 32, 'kind': 'tool', 'target': 'search',
                              'digest': 'd' * 64, 'requires_loaded': True}]
    malformed = call(tool='PluginCall', args={**PLUGIN, 'plugin_id': 5}, route=resolve)
    assert layers(malformed) == ['schema', 'route'] and len(resolve.calls) == 1


def test_loading_needs_a_reviewed_route_but_not_a_loaded_one():
    args = {k: PLUGIN[k] for k in ('plugin_id', 'tool', 'digest')}
    assert call(tool='LoadPluginTool', args=args, route=resolver(loaded=False)).allowed
    assert layers(call(tool='LoadPluginTool', args=args, route=resolver(reviewed=False))) == ['route']
    data = {'plugin_id': 'p' * 32, 'kind': 'prompt', 'identifier': 'summarize', 'digest': 'd' * 64, 'arguments': '{}'}
    resolve = resolver()
    assert call(tool='FetchPluginData', args=data, route=resolve).allowed
    assert resolve.calls[0]['kind'] == 'prompt' and resolve.calls[0]['target'] == 'summarize'
    assert layers(call(tool='FetchPluginData', args=data, route=resolver(kind='resource'))) == ['route']


def test_builtin_tools_never_consult_the_route_resolver():
    resolve = resolver()
    assert call(route=resolve).allowed and resolve.calls == []
    assert call(route=None).allowed


# Actor layer

@pytest.mark.parametrize('name,args', [
    ('Write', {'file_path': 'a', 'content': 'b'}),
    ('StartCommand', {'command': 'make'}),
    ('PluginCall', PLUGIN),
    ('LoadPluginTool', {'plugin_id': 'p', 'tool': 't', 'digest': 'd'}),
])
def test_child_cannot_call_root_only_tools_even_if_admitted(name, args):
    admitted = lambda actor: {t['function']['name'] for t in CATALOGUE}
    decision = call(tool=name, args=args, actor=Actor.child(RID, AGENT, 2), admitted=admitted,
                    run_config=config({**PERMS, 'tools': {**PERMS['tools'], 'terminal': True}}))
    assert layers(decision) == ['actor'] and 'requires the owning root run' in decision.reason


def test_native_only_routes_are_refused_for_every_model_actor():
    args = {'action_id': 'x'}
    for actor in (Actor.root(RID), Actor.child(RID, AGENT, 1), Actor.model()):
        decision = call(tool='approve', args=args, actor=actor, admitted=lambda actor: {'approve'})
        assert layers(decision) == ['actor'] and 'native-only route' in decision.reason
    assert call(tool='approve', args=args, actor=Actor.native()).allowed
    shouty = [tool('Approve', {}, [])]
    decision = call(tool='Approve', args={}, catalogue=shouty, catalogue_digest=catalogue_digest(shouty),
                    run_config=config(digest=catalogue_digest(shouty)))
    assert layers(decision) == ['actor']


def test_child_calls_only_its_admitted_set():
    seen = []

    def admitted(actor):
        seen.append(actor)
        return frozenset({'Read', 'SendAgentMessage'})
    child = Actor.child(RID, AGENT, 3)
    assert call(tool='Read', args={'file_path': 'a'}, actor=child, admitted=admitted).allowed
    assert call(tool='SendAgentMessage', args={'agent_id': 'x', 'text': 'y'}, actor=child, admitted=admitted).allowed
    decision = call(actor=child, admitted=admitted)
    assert layers(decision) == ['actor'] and "outside this child's admitted tools" in decision.reason
    assert seen[0] == child and decision.actor == {'kind': 'child', 'run_id': RID, 'agent_id': AGENT, 'generation': 3}
    assert 'No admitted tool set' in call(actor=child).reason
    assert 'malformed' in call(actor=child, admitted=lambda actor: 'Lookup').reason


def test_unowned_model_calls_get_no_root_only_tools():
    assert call(actor=Actor.model()).allowed
    decision = call(tool='Write', args={'file_path': 'a', 'content': 'b'}, actor=Actor.model())
    assert layers(decision) == ['actor'] and 'a model cannot call it' in decision.reason


@pytest.mark.parametrize('actor', [
    {'kind': 'native'}, 'native', None,
    Actor('root'), Actor('root', 'bad id'), Actor('child', RID, AGENT, 0), Actor('child', RID, AGENT, True),
    Actor('child', RID, None, 1), Actor('model', None, AGENT), Actor('native', None, None, 1), Actor('admin', RID),
])
def test_actor_identity_must_be_well_formed_host_state(actor):
    decision = call(actor=actor, admitted=lambda actor: {'Lookup'})
    assert layers(decision) == ['actor']
    assert decision.reason in ('The actor must come from host state, never from the request.',
                               'The actor identity is malformed.')


def test_identity_in_arguments_never_changes_the_actor():
    decision = call(tool='approve', args={'action_id': 'x', 'actor': 'native'}, actor=Actor.child(RID, AGENT, 1),
                    admitted=lambda actor: {'approve'})
    assert layers(decision) == ['schema', 'actor'] and decision.actor['kind'] == 'child'


# Policy layer

NARROW_AND_BROADEN = {'mode': 'full', 'tools': {**PERMS['tools'], 'terminal': True, 'edits': False}}


def test_broadening_after_pin_is_refused_while_narrowing_applies():
    common = dict(policy_version=4, policy=NARROW_AND_BROADEN)
    broadened = call(tool='RunCommand', args={'command': 'make'}, **common)
    assert layers(broadened) == ['policy'] and 'granted after this run started' in broadened.reason
    narrowed = call(tool='Write', args={'file_path': 'a', 'content': 'b'}, **common)
    assert layers(narrowed) == ['policy'] and 'narrowed during this run' in narrowed.reason
    unaffected = call(**common)
    assert unaffected.allowed and unaffected.policy_version == 4 and unaffected.pinned_policy_version == 3
    assert unaffected.effective_policy == {'mode': 'auto', 'tools': {**PERMS['tools'], 'terminal': False, 'edits': False}}


def test_mode_narrowing_applies_and_broadening_does_not():
    assert call(policy_version=4, policy={**PERMS, 'mode': 'full'}).effective_policy['mode'] == 'auto'
    assert call(policy_version=4, policy={**PERMS, 'mode': 'ask'}).effective_policy['mode'] == 'ask'


def test_broadened_run_works_after_explicit_repin():
    run_config = config()
    assert layers(call(tool='RunCommand', args={'command': 'make'}, run_config=run_config,
                       policy_version=4, policy=NARROW_AND_BROADEN)) == ['policy']
    run_config['authz'] = repin(run_config['authz'], catalogue_digest=DIGEST, policy_version=4)
    run_config['permissions'] = copy.deepcopy(NARROW_AND_BROADEN)
    decision = call(tool='RunCommand', args={'command': 'make'}, run_config=run_config,
                    policy_version=4, policy=NARROW_AND_BROADEN)
    assert decision.allowed and decision.effective_policy == NARROW_AND_BROADEN


def test_unversioned_permission_change_applies_only_its_narrowing():
    """Startup merges and out-of-band writes do not bump the version: content decides.
    A broadening never applies to a pinned run; a narrowing applies at once."""
    broadened = call(policy={**PERMS, 'mode': 'full'})
    assert broadened.allowed and broadened.effective_policy == PERMS
    assert call().allowed  # Lookup is in the granted 'files' group
    narrowed = {**PERMS, 'tools': {**PERMS['tools'], 'files': False}}
    refused = call(policy=narrowed)
    assert not refused.allowed and 'policy' in layers(refused) and 'narrowed' in dict(
        (r[0], r[1]) for r in refused.refusals)['policy']


@pytest.mark.parametrize('override,reason', [
    (dict(policy_version=2), 'moved backwards'),
    (dict(policy_version=4), 'current permissions are needed'),
    (dict(policy_version=True), 'current policy version is invalid'),
    (dict(policy_version=-1), 'current policy version is invalid'),
    (dict(policy_version='3'), 'current policy version is invalid'),
    (dict(run_config={'permissions': PERMS, 'authz': {'catalogue_digest': DIGEST}}), 'no pinned policy version'),
    (dict(run_config={'permissions': {'mode': 'root', 'tools': {}}, 'authz': pin(catalogue_digest=DIGEST, policy_version=3)}),
     'pinned permissions are malformed'),
    (dict(policy_version=4, policy={'mode': 'full', 'tools': {'files': 1}}), 'current permissions are malformed'),
])
def test_policy_version_rules_fail_closed(override, reason):
    decision = call(**override)
    assert 'policy' in layers(decision) and reason in dict((r[0], r[1]) for r in decision.refusals)['policy']


def test_permission_groups_gate_tools_and_ungrouped_tools_pass():
    assert 'disabled in Permissions' in call(tool='RunCommand', args={'command': 'ls'}).reason
    assert call(tool='SendAgentMessage', args={'agent_id': 'x', 'text': 'y'}).allowed
    unknown_group = call(groups={**GROUPS, 'Lookup': 'views'})
    assert layers(unknown_group) == ['policy'] and 'disabled in Permissions' in unknown_group.reason


def test_pin_and_repin_validation():
    assert pin(catalogue_digest=DIGEST, policy_version=0) == {'version': 1, 'catalogue_digest': DIGEST, 'policy_version': 0}
    for bad in (dict(catalogue_digest='x', policy_version=1), dict(catalogue_digest=DIGEST, policy_version=-1),
                dict(catalogue_digest=DIGEST, policy_version=True)):
        with pytest.raises(PolicyError):
            pin(**bad)
    first = pin(catalogue_digest=DIGEST, policy_version=5)
    with pytest.raises(PolicyError, match='backwards'):
        repin(first, catalogue_digest=DIGEST, policy_version=4)
    with pytest.raises(PolicyError, match='malformed'):
        repin({'policy_version': 5}, catalogue_digest=DIGEST, policy_version=6)
    second = repin(repin(first, catalogue_digest=DIGEST, policy_version=5), catalogue_digest=DIGEST, policy_version=6)
    assert second['generation'] == 2 and second['policy_version'] == 6


def test_layer_bugs_fail_closed_without_raising():
    def explode(name):
        raise KeyError(name)

    class Groups(dict):
        get = staticmethod(explode)
    decision = call(groups=Groups())
    assert layers(decision) == ['policy'] and decision.reason == 'The policy check failed closed (KeyError).'


# Consistency with the real host tables (read-only imports)

def test_root_only_matches_child_capabilities_and_effects():
    from pebble.agent_manager import ADMITTED
    from pebble.runtime import EFFECTS
    assert not ROOT_ONLY & ADMITTED
    assert EFFECTS - {'SpawnAgent'} <= ROOT_ONLY


def test_native_only_names_never_collide_with_catalogue_tools():
    names = {t['function']['name'].casefold() for t in canonical_catalogue()}
    assert not names & {n.casefold() for n in NATIVE_ONLY}


REAL = canonical_catalogue()
REAL_DIGEST = catalogue_digest(REAL)
REAL_NAMES = [t['function']['name'] for t in REAL]
# Every permission group open, including Phase 16's optional 'screen' group (off by default) and, D95, Phase 40's
# optional 'capture' group (on by default).
OPEN = {'mode': 'full', 'tools': dict.fromkeys(('files', 'edits', 'tests', 'apps', 'web', 'terminal', 'plugins', 'views', 'screen', 'capture'), True)}
# Integer arguments without a declared minimum or maximum. App.validate still
# implies 1-10000 for them; tool_authz enforces only what is declared (F5), so
# new unbounded integers should get explicit bounds instead of joining this list.
KNOWN_UNBOUNDED = {('Read', 'offset'), ('Read', 'limit'), ('WaitForAgents', 'after'), ('ReviewAgentResult', 'generation')}


def spec_of(name):
    return next(t for t in REAL if t['function']['name'] == name)['function']['parameters']


def example(prop):
    if 'enum' in prop:
        return prop['enum'][0]
    if prop['type'] == 'string':
        return 'x' * max(1, prop.get('minLength', 0))
    if prop['type'] == 'integer':
        return prop.get('minimum', prop.get('maximum', 0))
    return True


def echo_route(request):
    return {**{k: request[k] for k in ('plugin_id', 'kind', 'target', 'digest')}, 'reviewed': True, 'loaded': True}


def real_call(name, args, **override):
    values = dict(tool=name, args=args, actor=Actor.root(RID), catalogue=REAL, catalogue_digest=REAL_DIGEST,
                  run_config={'permissions': OPEN, 'authz': pin(catalogue_digest=REAL_DIGEST, policy_version=1)},
                  policy_version=1, route=echo_route)
    values.update(override)
    return check(**values)


def test_real_catalogue_is_canonical_and_fully_supported():
    assert len(REAL_NAMES) == len(set(REAL_NAMES)) >= 60
    assert catalogue_digest(list(reversed(REAL))) == REAL_DIGEST
    unbounded = {(t['function']['name'], key) for t in REAL
                 for key, prop in t['function']['parameters']['properties'].items()
                 if prop['type'] == 'integer' and ('minimum' not in prop or 'maximum' not in prop)}
    assert unbounded <= KNOWN_UNBOUNDED


def test_default_catalogue_and_groups_are_the_host_tables():
    decision = check(tool='Read', args={'file_path': 'a.py'}, actor=Actor.root(RID), catalogue_digest=REAL_DIGEST,
                     run_config={'permissions': OPEN, 'authz': pin(catalogue_digest=REAL_DIGEST, policy_version=1)},
                     policy_version=1)
    assert decision.allowed and decision.catalogue_digest == REAL_DIGEST
    closed = {**OPEN, 'tools': {**OPEN['tools'], 'files': False}}
    decision = check(tool='Read', args={'file_path': 'a.py'}, actor=Actor.root(RID), catalogue_digest=REAL_DIGEST,
                     run_config={'permissions': closed, 'authz': pin(catalogue_digest=REAL_DIGEST, policy_version=1)},
                     policy_version=1)
    assert layers(decision) == ['policy']


@pytest.mark.parametrize('name', REAL_NAMES)
def test_real_tool_generated_examples_pass_and_required_fields_are_enforced(name):
    spec = spec_of(name)
    props = spec['properties']
    minimal = {k: example(props[k]) for k in spec['required']}
    full = {k: example(p) for k, p in props.items()}
    assert strict_schema_errors(spec, minimal) == []
    assert strict_schema_errors(spec, full) == []
    assert real_call(name, full).allowed, real_call(name, full).refusals
    for key in spec['required']:
        missing = {k: v for k, v in full.items() if k != key}
        assert (key, 'is required') in strict_schema_errors(spec, missing)
        decision = real_call(name, missing)
        assert decision.refusal_layer == 'schema' and decision.field == key
    assert strict_schema_errors(spec, {**full, '__host_actor': 'native'}) == [('__host_actor', 'is not a declared argument')]
    assert real_call(name, {**full, 'host actor': 'native'}).field == '"host actor"'


@pytest.mark.parametrize('name', REAL_NAMES)
def test_real_tool_fields_refuse_spoofed_values(name):
    spec = spec_of(name)
    full = {k: example(p) for k, p in spec['properties'].items()}
    for key, prop in spec['properties'].items():
        def refused(value):
            errors = strict_schema_errors(spec, {**full, key: value})
            return [field for field, _ in errors] == [key]
        if prop['type'] == 'string':
            assert refused(1) and refused(None) and refused('a\x00b') and refused(['x'])
            assert refused('x' * (prop.get('maxLength', DEFAULT_MAX_STRING) + 1))
            if 'enum' in prop:
                assert refused(prop['enum'][0] + '-other')
        elif prop['type'] == 'integer':
            assert refused(True) and refused(False) and refused('1') and refused(1.0)
            if 'maximum' in prop:
                assert refused(prop['maximum'] + 1)
            if 'minimum' in prop:
                assert refused(prop['minimum'] - 1)
        else:
            assert refused(1) and refused(0) and refused('true')
