"""Phase 26: the 1.0.1 release ships no component without its notice, and the inventory counts what macOS builds.

The Phase 25 publish probe found two things. The frozen backend carried setuptools (MIT) with no notice: urllib3's
optional `backports.zstd` import resolved to setuptools' vendored backports, which pulled all of setuptools and its
runtime hook into the app, though nothing Pebble runs imports it. And release_inventory's inventory mode counted every
crate of Cargo.lock (191 never built for macOS, 8 more as differing versions), while the notices file is generated
from the macOS build's crates only (`notices --cargo-metadata`). The spec now excludes the setuptools family, and the
inventory takes the same --cargo-metadata filter; a crate the filter lists still needs its notice.
"""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('release_inventory_tests_24_for_26', Path(__file__).with_name(
    'test_release_inventory_24.py'))
t24 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t24)
inv = t24.inv
SETUPTOOLS_FAMILY = ('setuptools', 'pkg_resources', '_distutils_hack', 'distutils')


def metadata(tmp_path, *crates):
    path = tmp_path / 'cargo-metadata.json'
    path.write_text(json.dumps({'packages': [{'name': 'pebble', 'version': '1.0.1'}] +
                                           [{'name': n, 'version': v} for n, v in crates], 'version': 1}))
    return path


def test_the_inventory_counts_only_the_crates_the_macos_build_resolves(tmp_path):
    paths = t24.inputs(tmp_path)
    macos = metadata(tmp_path, ('serde', '1.0.228'))  # windows-sys is never built for macOS
    text, report = t24.generate(paths, cargo_metadata=macos)
    assert report['cargo_filtered_by_metadata'] and report['by_ecosystem']['cargo'] == 1
    source, _ = t24.stage_with(tmp_path, text)
    # Without the filter, the crate the notices rightly leave out is reported (the Phase 25 probe's 191 + 8).
    unfiltered = t24.run_inventory(tmp_path, paths, source)
    assert unfiltered['findings'] == [{'code': 'notice_missing', 'subject': 'cargo:windows-sys@0.59.0'}]
    # With the notices' own filter, the inventory is complete and names what it left out.
    filtered = t24.run_inventory(tmp_path, paths, source, cargo_metadata=macos)
    assert filtered['result'] == 'complete' and filtered['findings'] == []
    assert {c['name'] for c in filtered['components'] if c['ecosystem'] == 'cargo'} == {'serde'}
    record = filtered['inputs']['cargo_metadata']
    assert record['platform_crates'] == 1 and record['off_platform'] == ['windows-sys@0.59.0']
    assert record['sha256'] == inv.sha_bytes(macos.read_bytes())


def test_a_crate_the_filter_lists_still_needs_its_notice(tmp_path):
    paths = t24.inputs(tmp_path)
    text, _ = t24.generate(paths, cargo_metadata=metadata(tmp_path, ('serde', '1.0.228')))
    source, _ = t24.stage_with(tmp_path, text)
    both = metadata(tmp_path, ('serde', '1.0.228'), ('windows-sys', '0.59.0'))
    result = t24.run_inventory(tmp_path, paths, source, cargo_metadata=both)
    assert result['result'] == 'gaps'
    assert {'code': 'notice_missing', 'subject': 'cargo:windows-sys@0.59.0'} in result['findings']
    assert result['inputs']['cargo_metadata']['off_platform'] == []


def test_metadata_that_is_not_cargo_metadata_output_is_refused(tmp_path):
    paths = t24.inputs(tmp_path)
    text, _ = t24.generate(paths)
    source, _ = t24.stage_with(tmp_path, text)
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'crates': []}))
    try:
        t24.run_inventory(tmp_path, paths, source, cargo_metadata=bad)
    except inv.Refused as refusal:
        assert refusal.code == 'cargo_metadata_unsupported'
    else:
        raise AssertionError('non-metadata input was accepted')


def test_the_command_line_passes_the_filter():
    options = inv.parser().parse_args(['--stage-manifest', 'm.json', '--report', 'r.json', '--cargo-metadata', 'x.json'])
    assert options.cargo_metadata == 'x.json'
    source = (ROOT / 'scripts/release_inventory.py').read_text()
    assert 'requirements_lock=args.lock, cargo_metadata=args.cargo_metadata)' in source


def test_the_frozen_backend_leaves_the_setuptools_family_out():
    spec = (ROOT / 'build/pebble-backend.spec').read_text()
    excluded = set(re.findall(r"'([A-Za-z0-9_.]+)'", spec.split('EXCLUDED =')[1].split('LANGUAGES')[0]))
    assert set(SETUPTOOLS_FAMILY) <= excluded
    # `backports` itself stays importable to the analysis: PyInstaller's backports hook aliases it and refuses an
    # excluded target; with setuptools excluded the alias resolves to nothing and urllib3's optional import fails.
    assert 'backports' not in excluded
    flags = __import__('runpy').run_path(str(ROOT / 'scripts/package_backend.py'))['PYINSTALLER_FLAGS']
    assert set(SETUPTOOLS_FAMILY) <= {flags[i + 1] for i, flag in enumerate(flags) if flag == '--exclude-module'}
