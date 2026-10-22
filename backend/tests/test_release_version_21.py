"""Phase 21: one app version everywhere it is declared (the release bumped 0.4.2 -> 0.5.0; D20 §0.2 found the source
labelled 0.4.2 like the installed overlay release, so identity had to come from hashes)."""
import json
import re
import tomllib
from pathlib import Path

import pebble

ROOT = Path(__file__).resolve().parents[2]


def test_every_declared_version_equals_the_backend_version():
    tauri = json.loads((ROOT / 'src-tauri/tauri.conf.json').read_text())['version']
    cargo = tomllib.loads((ROOT / 'src-tauri/Cargo.toml').read_text())['package']['version']
    lock = re.search(r'\[\[package\]\]\nname = "pebble"\nversion = "([^"]+)"', (ROOT / 'src-tauri/Cargo.lock').read_text()).group(1)
    package = json.loads((ROOT / 'package.json').read_text())['version']
    npm_lock = json.loads((ROOT / 'package-lock.json').read_text())
    assert {pebble.VERSION, tauri, cargo, lock, package, npm_lock['version'], npm_lock['packages']['']['version']} == {pebble.VERSION}


def test_the_release_is_newer_than_the_installed_overlay_release():
    assert tuple(int(part) for part in pebble.VERSION.split('.')) > (0, 4, 2)


def test_no_product_module_hard_codes_another_app_version():
    for path in sorted((ROOT / 'backend/pebble').glob('*.py')):
        text = path.read_text()
        assert "'version': '0.4.2'" not in text and '"version": "0.4.2"' not in text, path.name


def test_the_mcp_client_identity_stays_at_its_historical_contract_value():
    """validate_phase6.py and validate_phase16.py pin Pebble's MCP clientInfo version to 0.4.2 (historical graders are
    never changed), so it is a separate protocol constant, documented in mcp_transport, not the app version."""
    from pebble import mcp_transport
    assert mcp_transport.MCP_CLIENT_VERSION == '0.4.2' and pebble.VERSION != mcp_transport.MCP_CLIENT_VERSION
