"""Phase 21 track V21-validator: scripts/validate_phase21.py, the phase21 service validator (release checks).

The unit tests start no backend. They pin the report contract (unique planned names in campaign order, a new report
created with O_EXCL, source digests inside the verifier's snapshot), the proposed verifier registration, the static
attach-route and version checks (each also against a hostile copy), the HEIC fixture the repository's pillow_heif
encodes, the frozen-layout walk on a synthetic dist, and the scripted provider.

Two always-run tests drive the entry point, main(), on the whole campaign against the source service: once as the
source run and once through an owned --backend launcher around the same source backend (the frozen plumbing, not a
frozen binary). The verifier runs the real frozen executable. No model inference and no network: the one model
turn is the validator's scripted loopback provider.
"""

import base64
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))  # the validator imports its sibling validators by name
_spec = importlib.util.spec_from_file_location('validate_phase21', SCRIPTS / 'validate_phase21.py')
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)


def load_script(name):
    spec = importlib.util.spec_from_file_location(name + '_for_phase21_validator', SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub(backend=None):
    """A campaign that never starts its provider or a service."""
    campaign = v.Campaign(['unused'], backend)
    campaign.provider.server.server_close()
    campaign.trap.server.server_close()
    return campaign


def data_url(image, fmt, mime):
    out = io.BytesIO()
    image.save(out, format=fmt)
    return 'data:' + mime + ';base64,' + base64.b64encode(out.getvalue()).decode()


def fixture_image(size=v.HEIC_SIZE):
    from PIL import Image
    image = Image.new('RGB', size, v.LEFT)
    image.paste(v.RIGHT, (size[0] // 2, 0, size[0], size[1]))
    return image


# The report contract --------------------------------------------------------------------------------------------

def test_planned_names_are_unique_ordered_and_every_scenario_is_bounded():
    assert len(v.PLANNED) == len(set(v.PLANNED)) and v.PLANNED[-1] == 'backend_inputs_stable_during_campaign'
    assert v.PLANNED[0] == 'fixture_scratch_created_outside_the_checkout'
    assert set(v.SCENARIO_BOUNDS) == set(v.SCENARIOS) and all(0 < b < v.BUDGET_SECONDS for b in v.SCENARIO_BOUNDS.values())
    assert sum(v.SCENARIO_BOUNDS.values()) <= v.BUDGET_SECONDS < v.VERIFIER_SECONDS == 240
    assert v.ATTACH_SECONDS < v.SCENARIO_BOUNDS['heic'] and v.SETTLE_SECONDS < v.SCENARIO_BOUNDS['heic']


def test_the_report_is_a_new_file_and_every_source_it_hashes_is_in_the_verifier_snapshot(tmp_path, monkeypatch, capsys):
    verify = load_script('verify_harness_candidate')
    # Names only: this copy need not hold .runtime/phase19-baseline/build_epoch8_fixture.py, which sources() hashes.
    monkeypatch.setattr(verify, 'sha', lambda path: None)
    covered = set(verify.sources())
    digests = v.checkout_digests()
    assert set(digests) <= covered, sorted(set(digests) - covered)
    assert all(isinstance(value, str) and len(value) == 64 for value in digests.values())
    for name in ('scripts/validate_phase21.py', 'scripts/validate_phase20.py', 'src-tauri/tauri.conf.json',
                 'src-tauri/Cargo.toml', 'package.json', 'backend/pebble/__init__.py', 'backend/pebble/images.py',
                 'src/App.vue', 'src-tauri/src/main.rs'):
        assert name in digests, name
    existing, link = tmp_path / 'report.json', tmp_path / 'link.json'
    existing.write_text('earlier evidence\n')
    link.symlink_to(tmp_path / 'missing-target.json')
    for path in (existing, link):
        with pytest.raises(SystemExit):
            v.main(['--report', str(path)])
        assert 'already exists' in capsys.readouterr().err
    with pytest.raises(FileExistsError):
        v.write_new(existing, 'replacement')
    with pytest.raises(OSError):
        v.write_new(link, 'through a link')
    assert existing.read_text() == 'earlier evidence\n' and not (tmp_path / 'missing-target.json').exists()
    # --backend must be an owned, executable regular file, never a link.
    target = tmp_path / 'pebble-backend'
    target.write_text('#!/bin/sh\n')
    target.chmod(0o755)
    (tmp_path / 'backend-link').symlink_to(target)
    for given, expected in ((tmp_path / 'backend-link', 'symbolic link'), (tmp_path / 'absent', 'not readable')):
        with pytest.raises(SystemExit):
            v.main(['--report', str(tmp_path / 'new.json'), '--backend', str(given)])
        assert expected in capsys.readouterr().err
    assert not (tmp_path / 'new.json').exists()
    # The report shape: source digests, the executable digest (null in source mode), unique names in planned order.
    campaign = stub()
    campaign.finish()
    report = v.build_report(campaign, backend=None, backend_sha=None, sources=digests, sources_after=dict(digests),
                            duration=1.0, scratch=tmp_path, keep=True)
    assert report['kind'] == 'source' and report['backend_sha256'] is None and report['source_sha256'] == digests
    assert [c['name'] for c in report['check_results']] == list(v.PLANNED) and report['all_planned_checks_recorded']
    assert report['passed'] is False and report['checks'] == ['backend_inputs_stable_during_campaign']
    assert report['duration_budget_seconds'] == 240 and report['scratch_preserved'] == str(tmp_path)
    changed = dict(digests, **{'scripts/validate_phase21.py': '0' * 64})
    campaign = stub()
    campaign.finish()
    moved = v.build_report(campaign, backend=None, backend_sha=None, sources=digests, sources_after=changed,
                           duration=1.0, scratch=tmp_path, keep=False)
    assert 'backend_inputs_stable_during_campaign' in moved['failed'] and moved['scratch_preserved'] is None
    # A frozen report binds the executable; a changed executable fails the stability check.
    campaign = stub(target)
    campaign.finish()
    frozen = v.build_report(campaign, backend=target, backend_sha='0' * 64, sources=digests, sources_after=dict(digests),
                            duration=1.0, scratch=tmp_path, keep=True)
    assert frozen['kind'] == 'frozen' and frozen['backend_sha256'] == '0' * 64
    assert 'backend_inputs_stable_during_campaign' in frozen['failed']


def phase20_namespace_unchanged(verify):
    """test_phase20_merge's namespace test in the rank-based form the track note proposes for it (as Phase 19's and
    Phase 16's tests already are): it holds whatever phases follow phase20."""
    rank = verify.PHASES.index('phase20')
    assert verify.PHASES[rank - 1:rank + 1] == ('phase19', 'phase20')
    own = [s for s in verify.SUITES if s.startswith('phase20')]
    assert own == ['phase20', 'phase20-faults'] and all((ROOT / 'scripts' / verify.SUITES[s]).is_file() for s in own)
    assert verify.default_suites('phase20') == verify.default_suites('phase19') + own
    now = {phase: verify.default_suites(phase) for phase in verify.PHASES[:rank]}
    suites, phases = dict(verify.SUITES), verify.PHASES
    try:
        verify.SUITES = {name: script for name, script in suites.items() if not name.startswith(phases[rank:])}
        verify.PHASES = phases[:rank]
        before = {phase: verify.default_suites(phase) for phase in verify.PHASES}
    finally:
        verify.SUITES, verify.PHASES = suites, phases
    return now == before


@pytest.mark.parametrize('applied', [False, True], ids=['proposed', 'applied'])
def test_the_proposed_phase21_registration_runs_only_in_the_phase21_namespace(applied):
    """The parent patch (track note): SUITES gains 'phase21' and PHASES ends with 'phase21'. Applied here to a loaded
    copy of the verifier only; no earlier campaign changes and the verifier's 240 s limit is the validator's budget.
    Once the parent has applied the patch, the applied registration must be the proposed one ('applied' stands in
    for that state until then)."""
    verify = load_script('verify_harness_candidate')
    if applied and 'phase21' not in verify.SUITES:
        verify.SUITES, verify.PHASES = {**verify.SUITES, 'phase21': 'validate_phase21.py'}, (*verify.PHASES, 'phase21')
    if 'phase21' in verify.SUITES or 'phase21' in verify.PHASES:
        # Phase 22 appends 'phase22' after phase21, so phase21 is located by position (same strength).
        rank = verify.PHASES.index('phase21')
        assert verify.SUITES.get('phase21') == 'validate_phase21.py' and verify.PHASES[rank - 1] == 'phase20'
    # phase21 and every namespace after it leave the tables; phase21 is then appended again.
    cut = verify.PHASES.index('phase21') if 'phase21' in verify.PHASES else len(verify.PHASES)
    dropped = tuple(verify.PHASES[cut:]) or ('phase21',)
    suites = {name: script for name, script in verify.SUITES.items() if not name.startswith(dropped)}
    phases = tuple(verify.PHASES[:cut])
    verify.SUITES, verify.PHASES = suites, phases
    before = {phase: verify.default_suites(phase) for phase in phases}
    verify.SUITES, verify.PHASES = {**suites, 'phase21': 'validate_phase21.py'}, (*phases, 'phase21')
    after = {phase: verify.default_suites(phase) for phase in verify.PHASES}
    assert {phase: after[phase] for phase in before} == before
    assert after['phase21'] == before['phase20'] + ['phase21']
    later = verify.PHASES[verify.PHASES.index('phase20') + 1:]
    assert [phase for phase in later if 'phase21'.startswith(phase)] == ['phase21']  # refused in the phase20 namespace
    assert (SCRIPTS / verify.SUITES['phase21']).is_file()
    source = (SCRIPTS / 'verify_harness_candidate.py').read_text(encoding='utf-8')
    assert 'process.wait(timeout=240)' in source and v.VERIFIER_SECONDS == 240
    # With phase21 registered, test_phase20_merge's pin PHASES[-2:] == ('phase19', 'phase20') no longer holds; its
    # rank-based form (the note's patch) does.
    assert verify.PHASES[-2:] == ('phase20', 'phase21') and phase20_namespace_unchanged(verify)


def test_checks_are_planned_unique_and_not_run_is_never_passed():
    campaign = stub()
    assert campaign.check('fixture_scratch_created_outside_the_checkout', lambda: campaign.need('missing')) is False
    assert 'Prerequisite' in campaign.checks[-1]['error']
    for bad in ('fixture_scratch_created_outside_the_checkout', 'an_unplanned_check'):
        with pytest.raises(AssertionError):
            campaign.check(bad, lambda: None)
    campaign.observations['scenario_errors'] = {'heic': 'Not started: its bound no longer fits.'}
    campaign.finish()
    recorded = {c['name']: c for c in campaign.checks}
    assert set(recorded) == set(v.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert not any(c['passed'] for c in campaign.checks)
    assert all('bound no longer fits' in recorded[name]['error'] for name in v.SCENARIOS['heic'])


def test_a_scenario_that_no_longer_fits_the_budget_is_not_started():
    campaign = stub()
    ran = []
    campaign.deadline = v.time.monotonic() + v.SCENARIO_BOUNDS['heic'] - 1
    campaign.scenario('heic', lambda: ran.append(True))
    assert not ran and 'Not started' in campaign.observations['scenario_errors']['heic']
    campaign.scenario('static', lambda: ran.append(True))
    assert ran == [True] and 'static' in campaign.observations['scenario_seconds']


# Static checks ----------------------------------------------------------------------------------------------------

def test_static_checks_pass_on_this_checkout():
    campaign = stub()
    campaign.static_checks()
    assert [c['name'] for c in campaign.checks] == list(v.SCENARIOS['static'])
    assert all(c['passed'] for c in campaign.checks), json.dumps([c.get('error') for c in campaign.checks])[:3000]
    import pebble
    declared = campaign.checks[1]['evidence']
    assert declared['version'] == pebble.VERSION and set(declared['declarations'].values()) == {pebble.VERSION}


@pytest.mark.parametrize('change, expected', [
    (('"apps","attach","attach_url"', '"apps","attach_url"'), 'does not allow the attachment routes'),
    (('&& !["state","new_chat","get_chat","send","cancel","open_url","attach",',
      '&& !["state","new_chat","get_chat","send","cancel","open_url",'), 'pet window cannot attach'),
])
def test_the_attach_route_check_fails_when_the_shell_stops_allowing_it(tmp_path, monkeypatch, change, expected):
    text = v.MAIN_RS.read_text(encoding='utf-8')
    changed = text.replace(*change, 1)
    assert changed != text, 'main.rs no longer has the anchor this test edits.'
    hostile = tmp_path / 'main.rs'
    hostile.write_text(changed, encoding='utf-8')
    monkeypatch.setattr(v, 'MAIN_RS', hostile)
    campaign = stub()
    campaign.static_checks()
    found = campaign.checks[0]
    assert not found['passed'] and expected in found['error'], found


def test_the_version_check_names_a_declaration_that_differs(tmp_path, monkeypatch):
    copy = tmp_path / 'checkout'
    for name in ('src-tauri/tauri.conf.json', 'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock', 'package.json',
                 'package-lock.json'):
        (copy / name).parent.mkdir(parents=True, exist_ok=True)
        (copy / name).write_bytes((ROOT / name).read_bytes())
    import pebble
    conf = json.loads((copy / 'src-tauri/tauri.conf.json').read_text())
    (copy / 'src-tauri/tauri.conf.json').write_text(json.dumps(dict(conf, version='0.4.2')))
    monkeypatch.setattr(v, 'ROOT', copy)
    found = v.declared_versions()
    assert found.pop('src-tauri/tauri.conf.json') == '0.4.2' and set(found.values()) == {pebble.VERSION}
    assert len(found) == 5  # Cargo.toml, package.json, Cargo.lock and package-lock.json's two version fields
    campaign = stub()
    campaign.static_checks()
    declared = campaign.checks[1]
    assert declared['name'] == 'v1_the_shell_cargo_and_npm_declare_the_backend_release_version'
    assert not declared['passed'] and "'src-tauri/tauri.conf.json': '0.4.2'" in declared['error'], declared


def test_the_service_version_check_fails_on_an_older_backend():
    class Older:
        facts = {}

        @staticmethod
        def rpc(method, **params):
            assert method == 'state'
            return {'version': '0.4.2'}
    campaign = stub()
    campaign.scenario_identity(Older)
    found = campaign.checks[0]
    assert found['name'] == 'v1_the_running_service_reports_the_release_version' and not found['passed']
    assert '0.4.2' in found['error']


# The HEIC fixture ---------------------------------------------------------------------------------------------------

def test_the_heic_fixture_is_encoded_by_the_repositorys_pillow_heif_and_its_copies_are_corrupt(tmp_path):
    import pillow_heif
    # V1-045 (Phase 24): pillow-heif is a developer-only fixture encoder; its pin lives in requirements-dev.txt.
    requirement = next(line for line in (ROOT / 'backend/requirements-dev.txt').read_text().splitlines()
                       if line.startswith('pillow-heif=='))
    made, facts = v.write_heic(tmp_path)
    assert facts['pillow_heif'] == pillow_heif.__version__ == requirement.split('==')[1]
    assert facts['brand'] in {'heic', 'heix', 'mif1'} and facts['boxes'][0] == 'ftyp' and 'mdat' in facts['boxes']
    photo = made['photo'].read_bytes()
    assert (made['photo'].stat().st_mode & 0o777) == 0o600 and len(photo) == facts['bytes']
    # The repository's decoder reads the fixture back at its size and colours (what the service's worker must do).
    from PIL import Image
    heif = pillow_heif.open_heif(io.BytesIO(photo))
    image = Image.frombytes(heif.mode, heif.size, bytes(heif.data), 'raw', heif.mode, heif.stride)
    assert v.matches(v.describe(data_url(image, 'PNG', 'image/png')))
    truncated, damaged = made['truncated'].read_bytes(), made['damaged'].read_bytes()
    assert photo.startswith(truncated) and len(truncated) < len(photo)
    assert len(damaged) == len(photo) and damaged[4:12] == photo[4:12] and damaged != photo
    for corrupt in (truncated, damaged):
        with pytest.raises(Exception):
            pillow_heif.open_heif(io.BytesIO(corrupt)).data
    with pytest.raises(FileExistsError):  # the fixture files are new files, never overwritten
        v.write_heic(tmp_path)


def test_describe_and_matches_read_size_and_colour_only():
    good = v.describe(data_url(fixture_image(), 'PNG', 'image/png'))
    assert good['mime'] == 'image/png' and good['format'] == 'PNG' and v.matches(good)
    assert set(good) == {'mime', 'format', 'width', 'height', 'left', 'right'}
    swapped = fixture_image()
    swapped.paste(v.LEFT, (v.HEIC_SIZE[0] // 2, 0, *v.HEIC_SIZE))
    assert not v.matches(v.describe(data_url(swapped, 'PNG', 'image/png')))
    assert not v.matches(v.describe(data_url(fixture_image((96, 160)), 'PNG', 'image/png')))
    assert 'error' in v.describe('data:image/png;base64,bm90IGFuIGltYWdl') and 'error' in v.describe(None)


@pytest.mark.parametrize('text, honest', [
    ('Could not decode this file. It may be damaged or use an unsupported encoding.', True),
    ('Could not read this file. It may be damaged or inaccessible.', True),
    ('Use an image smaller than 50 MB.', False),
    ('Use an image up to 40 megapixels and 8192 pixels per side.', False),
    ('Reading this file timed out. Try a smaller file.', False),
])
def test_an_honest_refusal_names_the_decode_and_no_false_limit(text, honest):
    assert bool(v.HONEST.search(text) and not v.FALSE_LIMIT.search(text)) is honest


# The frozen layout --------------------------------------------------------------------------------------------------

def test_the_frozen_layout_reports_links_and_the_pillow_heif_binaries_without_following_them(tmp_path):
    dist = tmp_path / 'dist'
    internal = dist / '_internal'
    (internal / 'pillow_heif' / '.dylibs').mkdir(parents=True)
    (internal / 'Python.framework' / 'Versions' / '3.14').mkdir(parents=True)
    (internal / 'pillow_heif' / '_pillow_heif.cpython-314-darwin.so').write_bytes(b'so')
    (internal / 'pillow_heif' / '.dylibs' / 'libheif.1.dylib').write_bytes(b'dylib')
    (internal / 'Python.framework' / 'Versions' / '3.14' / 'Python').write_bytes(b'python')
    (internal / 'libheif.1.dylib').symlink_to('pillow_heif/.dylibs/libheif.1.dylib')
    (internal / 'Python').symlink_to('Python.framework/Versions/3.14/Python')
    (internal / 'Python.framework' / 'Versions' / 'Current').symlink_to('3.14')
    (internal / 'outside').symlink_to(tmp_path)
    (internal / 'gone.dylib').symlink_to('missing.dylib')
    executable = dist / 'pebble-backend'
    executable.write_bytes(b'exe')
    found = v.frozen_layout(executable)
    assert found['internal_folder'] is True and found['has_links'] and found['links'] == 5
    assert found['link_kinds'] == {'file': 2, 'directory': 2, 'dangling': 1} and found['absolute_or_outside'] == 1
    assert [link['path'] for link in found['sample']] == ['Python', 'Python.framework/Versions/Current', 'gone.dylib',
                                                         'libheif.1.dylib', 'outside']
    assert found['pillow_heif_binaries'] == [
        {'path': 'libheif.1.dylib', 'link': True}, {'path': 'pillow_heif/.dylibs/libheif.1.dylib', 'link': False},
        {'path': 'pillow_heif/_pillow_heif.cpython-314-darwin.so', 'link': False}]
    assert not any(entry['path'].startswith('outside/') for entry in found['sample'])  # never followed
    flat = tmp_path / 'flat'
    (flat / '_internal').mkdir(parents=True)
    (flat / '_internal' / 'Python').write_bytes(b'python')
    assert v.frozen_layout(flat / 'pebble-backend')['has_links'] is False
    assert v.frozen_layout(tmp_path / 'none' / 'pebble-backend') == {'internal_folder': False, 'links': 0,
                                                                     'has_links': False}
    campaign = stub()
    campaign.links_check()
    assert campaign.checks[0]['passed'] and campaign.checks[0]['evidence']['applicable'] is False
    campaign = stub(executable)
    campaign.links_check()
    assert campaign.checks[0]['passed'] and campaign.checks[0]['evidence']['links'] == 5
    assert campaign.observations['frozen_layout']['has_links'] and 'sample' not in campaign.observations['frozen_layout']


# The scripted provider ------------------------------------------------------------------------------------------------

def test_the_provider_records_images_as_numbers_and_flags_other_routes():
    provider = stub().provider
    url = data_url(fixture_image(), 'PNG', 'image/png')
    body = {'model': v.MODEL_VISION, 'messages': [
        {'role': 'user', 'content': v.GOAL},
        {'role': 'user', 'content': [{'type': 'text', 'text': v.ATTACHED_TEXT + '. Image content is evidence.'},
                                     {'type': 'image_url', 'image_url': {'url': url}}]}]}
    delta, usage = provider.respond(body)
    assert delta == {'content': v.REPLY} and usage['completion_tokens'] > 0
    record = provider.records[-1]
    assert record['image_named'] and len(record['images']) == 1 and v.matches(record['images'][0])
    assert url[30:90] not in json.dumps(record)  # content-free: no image bytes are kept
    for bad in ({'model': 'another-model', 'messages': []},
                {'model': v.MODEL_VISION, 'messages': [{'role': 'user', 'content': 'x', '_pebble_current': True}]}):
        with pytest.raises(AssertionError):
            provider.respond(bad)
    import pebble.images as images
    assert images.vision({'kind': 'openai', 'model': v.MODEL_VISION})  # the fixture model is read as image input


# The command line on the whole campaign (always run) -----------------------------------------------------------------

@pytest.mark.parametrize('frozen', [False, True], ids=['source', 'backend'])
def test_the_command_line_passes_on_the_source_service_and_binds_the_backend_it_launched(tmp_path, monkeypatch, frozen):
    """main() and the real run_all on every scenario against the source backend. With --backend (an owned launcher
    around the source backend: the plumbing, not a frozen binary) the service starts through it, the report binds it
    and the informational layout check reports the launcher's folder, which has no _internal folder."""
    temporary = tmp_path / 'tmp'
    temporary.mkdir()
    monkeypatch.setattr(v.tempfile, 'tempdir', str(temporary))  # main()'s scratch, outside the checkout
    report_path, launcher, marker = tmp_path / 'report.json', tmp_path / 'bin' / 'pebble-backend', tmp_path / 'launched'
    argv = ['--report', str(report_path)]
    if frozen:
        launcher.parent.mkdir()
        launcher.write_text('#!/bin/sh\necho launched >> ' + json.dumps(str(marker)) + '\nexec ' + json.dumps(sys.executable)
                            + ' ' + json.dumps(str(ROOT / 'backend/main.py')) + ' "$@"\n')
        launcher.chmod(0o755)
        argv += ['--backend', str(launcher)]
    code = v.main(argv)
    assert (report_path.stat().st_mode & 0o777) == 0o600
    report = json.loads(report_path.read_text())
    results = {c['name']: c for c in report['check_results']}
    assert code == 0 and report['passed'] is True, json.dumps(
        {'failed': {name: results[name].get('error') for name in report['failed']}, 'fatal': report['fatal_error'],
         'scenario_errors': report['observations'].get('scenario_errors'),
         'stderr': report['backend_stderr_tail']})[:6000]
    assert report['kind'] == ('frozen' if frozen else 'source')
    assert report['checks'] == list(v.PLANNED) and not report['failed'] and report['all_planned_checks_recorded']
    assert report['backend_sha256'] == (v.sha(launcher.read_bytes()) if frozen else None)
    assert report['backend_path'] == (str(launcher.resolve()) if frozen else None)
    assert marker.is_file() is frozen  # the service started through the launcher, and only with --backend
    assert set(report['source_sha256']) == set(v.checkout_digests())
    import pebble
    assert results['v1_the_running_service_reports_the_release_version']['evidence']['service_version'] == pebble.VERSION
    layout = results['l1_the_frozen_backend_internal_folder_links_are_reported']['evidence']
    assert layout == ({'mode': 'frozen', 'applicable': True, 'internal_folder': False, 'links': 0, 'has_links': False}
                      if frozen else {'mode': 'source', 'applicable': False, 'reason': layout['reason']})
    decoded = results['h1_a_heic_photo_attached_as_the_ui_does_is_decoded_at_its_size']['evidence']
    assert decoded['size'] == list(v.HEIC_SIZE) and v.matches(decoded['preview'])
    presented = results['h1_the_decoded_photo_reaches_a_vision_request_and_the_chat_reports_its_dimensions']['evidence']
    assert presented['chat_dimensions'] == list(v.HEIC_SIZE) and presented['image_parts'] >= 1
    refused = results['h2_a_corrupt_heic_is_refused_honestly_and_the_service_keeps_working']['evidence']
    assert set(refused['refusals']) == {'truncated', 'damaged'} and refused['stored_rows_for_refused_files'] == 0
    assert report['synthetic_provider_attempts'] >= 1 and not report['provider_errors']
    assert all(set(r) == {'model', 'index', 'images', 'image_named', 'tools_offered', 'time'}
               for r in report['request_metadata'])
    services = report['observations']['services']
    assert [(s['label'], s['exit_code']) for s in services] == [('main', 0)]
    assert report['duration_seconds'] < v.BUDGET_SECONDS and report['scratch_preserved'] is None
    assert not list(temporary.iterdir())  # a passing run removes its scratch
