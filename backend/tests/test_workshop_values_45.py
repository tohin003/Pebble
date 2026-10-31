"""Phase 45 (1.9.5, F097 from audit round R3): a tool's test result equals what the test expects as a value, numbers as
numbers. The independent check failed a correct Celsius-to-Fahrenheit tool 0 of 5 (77.0 against 77 on JSON text), so
the simplest pure calculation was never kept on its own. The real sandboxed worker runs the audit's tool."""
import sys

import pytest

from pebble import code_worker
from pebble.code_mode import validate_tool

C_TO_F = 'def run(args):\n    return float(args["celsius"] * 9 / 5 + 32)\n'


@pytest.mark.parametrize('got, expect, same', [
    (77.0, 77, True), (98.60000000000001, 98.6, True), (-40.0, -40, True), (0.1 + 0.2, 0.3, True),
    (77.5, 77, False), (True, 1, False), (1, True, False), (True, True, True), ('77', 77, False),
    ([1.0, {'a': 2.0}], [1, {'a': 2}], True), ({'a': 1}, {'a': 1, 'b': 2}, False), (None, None, True), ([1], [1, 2], False),
])
def test_values_compare_as_values(got, expect, same):
    assert code_worker._same(got, expect) is same


@pytest.mark.skipif(sys.platform != 'darwin', reason='the worker runs under macOS sandbox-exec')
def test_the_audits_tool_passes_its_blind_tests_in_the_real_worker():
    blind = [{'args': {'celsius': 25}, 'expect': 77}, {'args': {'celsius': 0}, 'expect': 32},
             {'args': {'celsius': 100}, 'expect': 212}, {'args': {'celsius': -40}, 'expect': -40},
             {'args': {'celsius': 37}, 'expect': 98.6}]
    report = validate_tool(C_TO_F, blind, [], 1.0)
    assert [t['passed'] for t in report['tests']] == [True] * 5, report
    wrong = validate_tool(C_TO_F, [{'args': {'celsius': 25}, 'expect': 78}], [], 1.0)
    assert wrong['tests'][0]['passed'] is False and wrong['tests'][0]['got'] == '77.0'
