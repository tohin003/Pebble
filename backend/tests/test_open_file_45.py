"""Phase 45 (1.9.13, F200 from audit round R7): OpenApp opens a file in the app (Preview's Open panel, which lists the
person's folders, is no longer the only way in). The file exists, lies outside the protected places, and is checked
again just before it opens."""
from pathlib import Path

import pytest

from pebble import native_apps
from pebble.policy import PolicyError


def test_a_file_outside_the_protected_places_opens_in_the_app(tmp_path):
    picture = tmp_path / 'UXTEST-canvas.png'
    picture.write_bytes(b'\x89PNG\r\n\x1a\n')
    resolved = native_apps.document(str(picture), protected=[])
    assert resolved == str(picture.resolve())
    entry = {'id': '/System/Applications/Preview.app', 'document': resolved}
    assert native_apps.launch_argv(entry) == [native_apps.OPEN, '-a', '/System/Applications/Preview.app', resolved]
    assert native_apps.launch_argv(entry, True)[:3] == [native_apps.OPEN, '-g', '-a']
    assert native_apps.launch_argv({'id': '/System/Applications/Preview.app'}) == \
        [native_apps.OPEN, '-a', '/System/Applications/Preview.app'], 'without a path, as before'


def test_missing_relative_folder_and_protected_paths_are_refused(tmp_path):
    keys = tmp_path / 'Library' / 'Keychains'
    keys.mkdir(parents=True)
    (keys / 'login.keychain-db').write_bytes(b'x')
    for bad, why in ((str(tmp_path / 'missing.png'), 'does not exist'), ('canvas.png', 'full path'),
                     (str(tmp_path), 'Only a file'), (str(keys / 'login.keychain-db'), 'protected place')):
        with pytest.raises(PolicyError, match=why):
            native_apps.document(bad, protected=[keys])


def test_the_receipt_names_the_file_and_the_app():
    said = native_apps.describe({'requested': 'Preview', 'launch_observed': True, 'document': 'UXTEST-canvas.png'})
    assert said == 'macOS accepted the request to open UXTEST-canvas.png in Preview (running: observed).'


def test_openapp_offers_a_path_and_screenrequest_points_to_it():
    from pebble.screen_session import REQUEST_SCHEMA
    from pebble.tool_catalogue import TOOLS
    spec = next(t for t in TOOLS if t['function']['name'] == 'OpenApp')['function']
    assert spec['parameters']['properties']['path'] == {'type': 'string', 'maxLength': 1024}
    assert 'use OpenApp with its path' in REQUEST_SCHEMA['function']['description']
