"""Phase 32 (D50): the GPT benchmark reads the ChatGPT plan's usage windows before each batch and never starts a
batch into an exhausted window (a refused cell would be scored as a failed task)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmarks.drivers import chatgpt_limits as cl  # noqa: E402

READING = {'ordinaryUsageAllowed': True, 'rateLimits': {
    'planType': 'plus', 'rateLimitReachedType': None,
    'primary': {'usedPercent': 12, 'windowDurationMins': 300, 'resetsAt': 2_000_000},
    'secondary': {'usedPercent': 3, 'windowDurationMins': 10080, 'resetsAt': 2_500_000}}}


def test_a_reading_keeps_only_the_numbers():
    parsed = cl.parse(READING)
    assert parsed == {'plan': 'plus', 'primary': {'used_percent': 12, 'window_minutes': 300, 'resets_at': 2_000_000},
                      'secondary': {'used_percent': 3, 'window_minutes': 10080, 'resets_at': 2_500_000},
                      'limit_reached': None, 'ordinary_usage_allowed': True}
    for bad in (None, {}, {'rateLimits': {}}, {'rateLimits': {'primary': {'usedPercent': 'x'}}}):
        with pytest.raises(cl.LimitsUnavailable):
            cl.parse(bad)


def reading(primary, weekly=3, reached=None, allowed=True):
    return {'plan': 'plus', 'limit_reached': reached, 'ordinary_usage_allowed': allowed,
            'primary': {'used_percent': primary, 'window_minutes': 300, 'resets_at': 10_000},
            'secondary': {'used_percent': weekly, 'window_minutes': 10080, 'resets_at': 900_000}}


def test_the_batch_rule():
    assert cl.decide(reading(79), now=9_000)['action'] == 'go'
    wait = cl.decide(reading(80), now=9_000)
    assert wait['action'] == 'wait' and wait['seconds'] == 1_060  # to the reset, plus a minute
    assert cl.decide(reading(10, weekly=90), now=9_000)['action'] == 'stop'
    assert cl.decide(reading(95), now=10_000 - cl.MAX_WAIT - 10)['action'] == 'stop'  # a wait beyond six hours
    assert cl.decide(reading(10, reached='primary'), now=9_000)['action'] == 'wait'
    assert cl.decide(reading(10, allowed=False), now=20_000)['action'] == 'stop'  # reached, and no reset ahead


def test_read_limits_speaks_to_the_app_server_with_a_scratch_home(tmp_path):
    """A stand-in app server: it must be started as `<codex> app-server` with CODEX_HOME and a scratch HOME, get
    initialize, initialized and account/rateLimits/read, and nothing else."""
    script = tmp_path / 'codex'
    log = tmp_path / 'seen.jsonl'
    script.write_text('#!/usr/bin/python3\n'
                      'import json, os, sys\n'
                      f'log = open({str(log)!r}, "a")\n'
                      'log.write(json.dumps({"argv": sys.argv[1:], "home": os.environ["HOME"], "codex_home": os.environ["CODEX_HOME"]}) + "\\n")\n'
                      'for line in sys.stdin:\n'
                      '    m = json.loads(line); log.write(json.dumps({"method": m.get("method")}) + "\\n"); log.flush()\n'
                      '    if m.get("id") == 1: print(json.dumps({"id": 1, "result": {}}), flush=True)\n'
                      '    if m.get("id") == 2: print(json.dumps({"id": 2, "result": json.loads(' + repr(json.dumps(READING)) + ')}), flush=True)\n')
    script.chmod(0o755)
    home = tmp_path / 'codex-home'
    home.mkdir()
    assert cl.read_limits(script, home, timeout=10)['primary']['used_percent'] == 12
    seen = [json.loads(line) for line in log.read_text().splitlines()]
    assert seen[0]['argv'] == ['app-server'] and seen[0]['codex_home'] == str(home)
    assert seen[0]['home'].startswith(str(home)) and Path(seen[0]['home']).name == 'limits-home'
    assert [s['method'] for s in seen[1:]] == ['initialize', 'initialized', 'account/rateLimits/read']
