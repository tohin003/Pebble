"""Child-only user code entry point. Never imported for execution in the service."""
import json
import resource
import sys


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32_768, 32_768))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    payload = json.loads(sys.stdin.buffer.read(48_001))
    scope = {'__name__': '__pebble_hook__'}
    exec(compile(payload['source'], '<reviewed-hook>', 'exec'), scope)
    result = scope['handle'](payload['event'])
    print(json.dumps(result, allow_nan=False, separators=(',', ':')))
