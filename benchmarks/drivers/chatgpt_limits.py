"""Phase 32 (D50): the ChatGPT plan's usage windows, read before each batch of GPT benchmark cells.

Both products of a `codex` plan run on one ChatGPT account (Pebble through its evaluation sign-in, Codex through its
scratch CODEX_HOME), so a batch must never start into an exhausted window: a cell refused for usage would be scored as
a failed task. Codex's own app server answers `account/rateLimits/read` without a model request: the 5-hour (primary)
and weekly (secondary) windows' used percent and reset time, and the plan type. Nothing here sends a prompt, and no
token or account content leaves the process; only the numbers are kept.

decide() turns a reading into the batch rule the runner applies: go below the pause line; wait for the primary reset
when it is at or above it; stop when the weekly window is at or above its line, or a wait would exceed MAX_WAIT.
"""
import json
import os
import select
import subprocess
import time

PAUSE_AT = 80  # percent of the 5-hour window
STOP_WEEKLY_AT = 90  # percent of the weekly window
MAX_WAIT = 6 * 3600
PATH = '/usr/bin:/bin:/usr/sbin:/sbin'


class LimitsUnavailable(RuntimeError):
    """The app server did not answer with usage windows."""


def _window(raw):
    if not isinstance(raw, dict):
        return None
    used, minutes, resets = raw.get('usedPercent'), raw.get('windowDurationMins'), raw.get('resetsAt')
    if not all(type(v) in (int, float) for v in (used, minutes, resets)):
        return None
    return {'used_percent': used, 'window_minutes': minutes, 'resets_at': int(resets)}


def parse(result):
    """The usage windows from an `account/rateLimits/read` result, or LimitsUnavailable."""
    limits = (result or {}).get('rateLimits') if isinstance(result, dict) else None
    primary = _window((limits or {}).get('primary'))
    if not isinstance(limits, dict) or primary is None:
        raise LimitsUnavailable('no_usage_windows')
    return {'plan': limits.get('planType'), 'primary': primary, 'secondary': _window(limits.get('secondary')),
            'limit_reached': limits.get('rateLimitReachedType'), 'ordinary_usage_allowed': result.get('ordinaryUsageAllowed')}


def read_limits(binary, codex_home, *, timeout=30, spawn=subprocess.Popen):
    """One reading through `codex app-server` with a scratch HOME; the sign-in is CODEX_HOME's, never ~/.codex."""
    home = os.path.join(str(codex_home), 'tmp', 'limits-home')
    os.makedirs(home, mode=0o700, exist_ok=True)
    env = {'PATH': PATH, 'HOME': home, 'CODEX_HOME': str(codex_home), 'LANG': 'en_US.UTF-8',
           'USER': os.environ.get('USER', 'bench')}
    process = spawn([str(binary), 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=env, text=True)
    try:
        def send(message):
            process.stdin.write(json.dumps(message) + '\n')
            process.stdin.flush()

        def answer(ident):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if not ready:
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get('id') == ident:
                    return message
            raise LimitsUnavailable('no_answer')

        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'pebble-bench', 'version': '1'}}})
        answer(1)
        send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'account/rateLimits/read', 'params': {}})
        reply = answer(2)
        if 'error' in reply:
            raise LimitsUnavailable('refused')
        return parse(reply.get('result'))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def decide(limits, *, now=None, pause_at=PAUSE_AT, stop_weekly_at=STOP_WEEKLY_AT, max_wait=MAX_WAIT):
    """{'action': 'go'|'wait'|'stop', 'seconds': wait, 'reason': ...} for the next batch."""
    now = time.time() if now is None else now
    weekly = limits.get('secondary')
    if limits.get('limit_reached') or limits.get('ordinary_usage_allowed') is False:
        wait = limits['primary']['resets_at'] - now
        return ({'action': 'wait', 'seconds': max(0, int(wait)) + 60, 'reason': 'limit_reached'} if 0 < wait <= max_wait
                else {'action': 'stop', 'seconds': 0, 'reason': 'limit_reached'})
    if weekly and weekly['used_percent'] >= stop_weekly_at:
        return {'action': 'stop', 'seconds': 0, 'reason': 'weekly_window_at_%d' % weekly['used_percent']}
    if limits['primary']['used_percent'] >= pause_at:
        wait = limits['primary']['resets_at'] - now
        if wait > max_wait:
            return {'action': 'stop', 'seconds': 0, 'reason': 'wait_beyond_max'}
        return {'action': 'wait', 'seconds': max(0, int(wait)) + 60, 'reason': 'primary_window_at_%d' % limits['primary']['used_percent']}
    return {'action': 'go', 'seconds': 0, 'reason': 'primary_window_at_%d' % limits['primary']['used_percent']}


__all__ = ['LimitsUnavailable', 'decide', 'parse', 'read_limits']
