"""Phase 30 (D41, V1-015): benchmark runs never use a backend with the installed app's identity.

The Claude benchmark ran Pebble, and stored its Anthropic key, through the release bundle's backend. That bundle is
com.pebble.local with the vault helper, so it kept its secrets in the person's own Keychain item, beside the installed
app's. The candidate's test bundle holds the same bytes under com.pebble.candidate and keeps per-item records.
"""
import hashlib
import importlib.util
import plistlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmarks import plan_24a as bp  # noqa: E402
from benchmarks.drivers import live  # noqa: E402

spec = importlib.util.spec_from_file_location('bench_claude_identity_30', ROOT / 'scripts/bench_claude.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
PAYLOAD = b'the same candidate backend bytes'


def bundle(root, identifier):
    app = root / (identifier + '.app')
    (app / 'Contents').mkdir(parents=True)
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': identifier}))
    backend = app / 'Contents/Resources/resources/backend/pebble-backend'
    backend.parent.mkdir(parents=True)
    backend.write_bytes(PAYLOAD)
    return backend


def plan():
    return {'candidate': {'backend_sha256': hashlib.sha256(PAYLOAD).hexdigest()}}


def test_the_release_bundle_is_refused_and_the_test_bundle_with_the_same_bytes_runs(tmp_path):
    release, test = bundle(tmp_path / 'release', 'com.pebble.local'), bundle(tmp_path / 'test', 'com.pebble.candidate')
    with pytest.raises(bp.PlanRefused, match='candidate_has_the_installed_identity'):
        live.candidate_command(plan(), release)
    assert live.candidate_command(plan(), test) == [str(test.resolve())]
    loose = tmp_path / 'build' / 'pebble-backend'
    loose.parent.mkdir()
    loose.write_bytes(PAYLOAD)
    assert live.candidate_command(plan(), loose) == [str(loose.resolve())], 'a backend outside any bundle still runs'


def test_a_link_into_the_release_bundle_is_refused(tmp_path):
    release = bundle(tmp_path / 'release', 'com.pebble.local')
    link = tmp_path / 'pebble-backend'
    link.symlink_to(release)
    with pytest.raises(bp.PlanRefused, match='candidate_has_the_installed_identity'):
        live.evaluation_backend(link)


@pytest.mark.parametrize('name', ['eval_provider', 'finish_provider'])
def test_key_entry_and_its_finish_refuse_the_release_bundle_before_anything_starts(tmp_path, name):
    release = bundle(tmp_path / 'release', 'com.pebble.local')
    started, asked = [], []

    class Measure:
        class Backend:
            def __init__(self, argv, home):
                started.append(argv)

    kwargs = dict(backend=release, home=tmp_path / 'eval-home', ledger=None, metadata=tmp_path / 'eval.json',
                  model='claude-opus-5-5', measure=Measure, installed=[])
    if name == 'eval_provider':
        kwargs['ask'] = lambda prompt: asked.append(prompt) or 'never-asked'
    with pytest.raises(bp.PlanRefused, match='candidate_has_the_installed_identity'):
        getattr(cli, name)(**kwargs)
    assert started == [] and asked == [] and not (tmp_path / 'eval-home').exists()
