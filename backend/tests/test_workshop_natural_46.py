"""Phase 46 (D154, D164): make_tool accepts the shapes a model writes unprompted (workshop.natural).

Measured first: on golden dev d-wr-01 (gpt-6.1-sol, tree r23-sol) both first make_tool calls of the session failed on
their shapes alone (a function named after the tool taking keyword arguments, inputs as {name: type}, tests keyed
inputs/expected, one or two tests), and each failure cost a 12-20 s request; the second also left the conversation
without a tool for its later turns. The tools below are those two calls, word for word.
"""
import sys

import pytest

from pebble import workshop as ws
from test_workshop_41 import FakeApp, make

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='the validation worker needs the macOS sandbox')

IBAN_CHECK = '''def iban_check(value, lengths):
    s = value.replace(" ", "").upper()
    if len(s) < 4 or not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in s):
        return {"valid": False}
    rotated = s[4:] + s[:4]
    digits = "".join(str(ord(c)-55) if "A" <= c <= "Z" else c for c in rotated)
    remainder = int(digits) % 97
    return {"length": len(s), "expected": lengths.get(s[:2]), "remainder": remainder, "valid": len(s) == lengths.get(s[:2]) and remainder == 1}
'''
IBAN_VALID = '''def iban_valid(iban, lengths):
    s = "".join(iban.split()).upper()
    if len(s) != lengths.get(s[:2]):
        return False
    digits = "".join(str(ord(c)-55) if "A" <= c <= "Z" else c for c in s[4:] + s[:4])
    return digits.isdecimal() and int(digits) % 97 == 1'''
GB = 'GB82 WEST 1234 5698 7654 32'


def test_the_first_calls_sol_wrote_are_made_at_once():
    app = FakeApp()
    first, mine = make(app, name='iban_check', purpose='Check normalized IBAN length and ISO 13616 mod-97 remainder',
                       code=IBAN_CHECK, inputs={'value': 'string', 'lengths': 'object'},
                       tests=[{'inputs': {'value': GB, 'lengths': {'GB': 22}},
                               'expected': {'length': 22, 'expected': 22, 'remainder': 1, 'valid': True}}])
    assert first['made'], first
    assert 'def run(args):\n    return iban_check(**args)\n' in mine['iban_check']
    assert 'Pebble keeps it on its own only with 3+ tests' in first['note']
    spec = ws._made(app, 'r1')['iban_check']['spec']
    assert spec['inputs'] == {'type': 'object', 'properties': {'value': {'type': 'string'}, 'lengths': {'type': 'object'}},
                              'required': ['value', 'lengths']}
    assert spec['tests'] == [{'args': {'value': GB, 'lengths': {'GB': 22}},
                              'expect': {'length': 22, 'expected': 22, 'remainder': 1, 'valid': True}}]
    second, _ = make(app, name='iban_valid', purpose='Check country length and ISO 13616 mod-97 after ignoring '
                     'whitespace and case', code=IBAN_VALID, inputs={'iban': 'string', 'lengths': 'object'},
                     tests=[{'inputs': {'iban': GB, 'lengths': {'GB': 22}}, 'expected': True},
                            {'inputs': {'iban': 'GB83 WEST 1234 5698 7654 32', 'lengths': {'GB': 22}}, 'expected': False}])
    assert second['made'], second


def test_a_tool_taking_one_args_object_and_python_type_names():
    code, tests, inputs = ws.natural('add_pair', "def add_pair(args):\n    return args['a'] + args['b']\n",
                                     [{'input': {'a': 1, 'b': 2}, 'output': 3}], {'a': 'int', 'b': {'type': 'int'}})
    assert code.endswith('def run(args):\n    return add_pair(args)\n')
    assert tests == [{'args': {'a': 1, 'b': 2}, 'expect': 3}]
    assert inputs['properties'] == {'a': {'type': 'integer'}, 'b': {'type': 'integer'}} and inputs['required'] == ['a', 'b']


def test_the_canonical_shapes_and_anything_unrecognised_are_left_for_the_checks():
    canonical = ("def run(args):\n    return 1\n", [{'args': {}, 'expect': 1}],
                 {'type': 'object', 'properties': {'a': {'type': 'integer'}}})
    assert ws.natural('a_tool', *canonical) == canonical
    other = ws.natural('a_tool', 'def go(args):\n    return 1\n', None, {'a': ['x']})
    assert other == ('def go(args):\n    return 1\n', None, {'a': ['x']})  # not named after the tool: run(args) asked for
    assert any('run(args)' in f for f in ws.static_findings('a_tool', 'x', other[0]))
    assert ws.schema_findings(ws.natural('a_tool', None, None, {'a': 'blob'})[2])  # an unknown type is still named
    result, mine = make(FakeApp(), tests=[])
    assert not result['made'] and mine is None and any('Give 1-20 tests' in f for f in result['findings'])


def test_pebble_never_keeps_a_tool_with_fewer_than_three_tests_on_its_own():
    app = FakeApp()
    made, _ = make(app, tests=[{'args': {'a': 1, 'b': 2}, 'expect': 3}])
    assert made['made']
    ws._made(app, 'r1')['add_pair']['uses'] = 1
    kept = ws.keep(app, 'r1', 'c1', {'name': 'add_pair', 'reason': 'used'}, None,
                   complete=lambda app, messages: pytest.fail('no blind check for a tool Pebble will not keep alone'))
    assert kept['asked'] and any('only 1 test(s) of its own' in why for why in kept['why']), kept
