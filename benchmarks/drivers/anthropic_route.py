"""The evaluation provider for ``claude`` plans (Phase 27, D20): the person's Anthropic API key, never the app's.

The ChatGPT evaluation connection (V1-015, pebble.evaluation) stays ChatGPT-only. This is its API-key counterpart for
the benchmark: `scripts/bench_claude.py eval-provider` starts the frozen candidate backend in an evaluation home,
reads the key from the person's own hidden prompt (getpass), and passes it once to the backend's model_save, so the
backend writes its own per-profile Keychain record and is the only program that ever reads it again. This module
validates that profile, records it in the private ledger as fingerprints only (exclusive create), and refuses any
profile id or connection id the installed app holds (its settings are read read-only, ids only), so a benchmark can
never run on the installed app's provider.
"""

import hmac
import json
import os
import re
import stat
from pathlib import Path

from pebble import evaluation as ev
from pebble.evaluation import EvaluationError

PROVIDER_FILE = 'evaluation-provider.json'
PROVIDER_SCHEMA = 'pebble.bench_evaluation_provider/1'
ANTHROPIC_URL = 'https://api.anthropic.com/v1'
ALLOWED_HOSTS = frozenset({'api.anthropic.com', 'localhost', '127.0.0.1', '::1'})
KEY_SHAPE = re.compile(r'sk-ant-[A-Za-z0-9_\-]{20,300}')
SECRETS = re.compile(r'(?i)sk-ant-|"(api_key|key|token|secret|password)"\s*:')


def validate_provider_profile(profile, model=None):
    """An Anthropic API-key profile on the official endpoint, its key in Keychain (has_key), nothing local."""
    if not isinstance(profile, dict) or not 1 <= len(profile) <= 64:
        raise EvaluationError('provider_profile_invalid')
    text = json.dumps(profile, sort_keys=True)
    if SECRETS.search(text):
        raise EvaluationError('provider_profile_holds_a_secret')
    if profile.get('kind') != 'anthropic' or profile.get('url') != ANTHROPIC_URL:
        raise EvaluationError('provider_route_must_be_anthropic')
    for name in ('id', 'connection_id', 'model'):
        if not isinstance(profile.get(name), str) or not 1 <= len(profile[name]) <= 200:
            raise EvaluationError('provider_identity_missing')
    if profile.get('has_key') is not True or profile.get('path') or profile.get('local'):
        raise EvaluationError('provider_key_or_local_route_invalid')
    if type(profile.get('context')) is not int or profile['context'] < 4096:
        raise EvaluationError('provider_profile_invalid')
    if model is not None and profile['model'] != model:
        raise EvaluationError('provider_model_not_the_plan_model')
    return dict(profile)


def load_provider_metadata(path):
    """The private metadata file eval-provider wrote: {"profile": {...}} only, owned by this user, mode 0600."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError:
        raise EvaluationError('provider_metadata_missing') from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EvaluationError('provider_metadata_not_regular_file')
    if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
        raise EvaluationError('provider_metadata_not_private')
    if info.st_size > 65536:
        raise EvaluationError('provider_metadata_too_large')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        raise EvaluationError('provider_metadata_invalid') from None
    if not isinstance(data, dict) or set(data) != {'profile'}:
        raise EvaluationError('provider_metadata_unexpected_fields')
    return {'profile': validate_provider_profile(data['profile'])}


def _refuse_installed(profile, installed):
    ids = installed['connection_ids']
    if profile['id'] in ids or profile['connection_id'] in ids:
        raise EvaluationError('installed_app_provider_refused')


def _body(ledger, profile):
    return {'schema': PROVIDER_SCHEMA, 'connection': ledger.fingerprint('provider-connection:' + profile['connection_id']),
            'profile': ledger.fingerprint('provider-profile:' + profile['id']),
            'model': ledger.fingerprint('provider-model:' + profile['model'])}


def record_provider(ledger, profile, *, installed=None):
    """Record, once per ledger, the evaluation provider profile. Content-free: fingerprints only."""
    profile = validate_provider_profile(profile)
    _refuse_installed(profile, ev.installed_connection_identities() if installed is None else installed)
    body = {**_body(ledger, profile), 'recorded_at': ev._now()}
    record = {**body, 'mac': ledger.fingerprint('evaluation-provider:' + ev._canonical(body))}
    path = ledger.root / PROVIDER_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        raise EvaluationError('evaluation_provider_already_recorded') from None
    try:
        os.write(fd, (ev._canonical(record) + '\n').encode())
    finally:
        os.close(fd)
    return {'evaluation_provider': 'recorded'}


def _record(ledger):
    path = ledger.root / PROVIDER_FILE
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        raise EvaluationError('evaluation_provider_not_recorded') from None
    except OSError:
        raise EvaluationError('evaluation_provider_record_invalid') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
            raise EvaluationError('evaluation_provider_record_invalid')
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        record = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise EvaluationError('evaluation_provider_record_invalid') from None
    if not isinstance(record, dict) or record.get('schema') != PROVIDER_SCHEMA or not isinstance(record.get('mac'), str):
        raise EvaluationError('evaluation_provider_record_invalid')
    body = {k: v for k, v in record.items() if k != 'mac'}
    if not hmac.compare_digest(record['mac'], ledger.fingerprint('evaluation-provider:' + ev._canonical(body))):
        raise EvaluationError('evaluation_provider_record_tampered')
    return record


def require_provider(metadata, ledger, model, *, installed=None):
    """Refuse a live claude-plan run unless ``metadata`` is the ledger's recorded evaluation provider, on the plan's
    model, and none of its ids is the installed app's. Returns a content-free summary."""
    profile = validate_provider_profile((metadata or {}).get('profile'), model=model)
    installed = ev.installed_connection_identities() if installed is None else installed
    _refuse_installed(profile, installed)
    record, expected = _record(ledger), _body(ledger, profile)
    if any(not hmac.compare_digest(str(record.get(key)), expected[key]) for key in ('connection', 'profile', 'model')):
        raise EvaluationError('evaluation_provider_mismatch')
    return {'evaluation_provider': 'separate', 'installed_stores_checked': installed['stores']}


def key_shape_ok(key):
    """Whether a typed key looks like an Anthropic key; the key itself is never printed, logged or stored here."""
    return isinstance(key, str) and bool(KEY_SHAPE.fullmatch(key.strip()))


__all__ = ['ALLOWED_HOSTS', 'ANTHROPIC_URL', 'key_shape_ok', 'load_provider_metadata', 'record_provider',
           'require_provider', 'validate_provider_profile']
