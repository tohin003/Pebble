import sys

import pytest

from pebble import platform_providers
from pebble.platform_providers import KINDS, MISSING, REASON, provider, require
from pebble.policy import PolicyError


@pytest.fixture
def fake_sandbox(tmp_path, monkeypatch):
    binary = tmp_path / 'sandbox-exec'
    binary.write_text('#!/bin/sh\nexit 1\n')
    binary.chmod(0o755)
    monkeypatch.setattr(platform_providers, 'SANDBOX_EXEC', str(binary))
    return binary


@pytest.mark.parametrize('platform', ['linux', 'win32', 'linux2', 'cygwin', 'freebsd14', 'emscripten', 'Darwin'])
def test_other_platforms_always_refuse(monkeypatch, fake_sandbox, platform):
    # Even with a sandbox-exec look-alike present, no other platform is supported.
    monkeypatch.setattr(sys, 'platform', platform)
    expected = {'platform': platform, 'supported': False, 'confinement': None, 'reason': REASON}
    assert provider() == expected
    assert provider(platform) == expected
    for kind in KINDS:
        with pytest.raises(PolicyError) as error:
            require(kind)
        assert str(error.value) == REASON == 'no verified isolation on this platform; execution stays disabled'


def test_darwin_with_sandbox_exec_is_supported(monkeypatch, fake_sandbox):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    assert provider() == {'platform': 'darwin', 'supported': True, 'confinement': 'sandbox-exec', 'reason': None}
    for kind in KINDS:
        assert require(kind) == {'platform': 'darwin', 'supported': True, 'confinement': 'sandbox-exec',
                                 'reason': None, 'kind': kind}


def test_darwin_without_sandbox_exec_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(platform_providers, 'SANDBOX_EXEC', str(tmp_path / 'missing'))
    assert provider() == {'platform': 'darwin', 'supported': False, 'confinement': None, 'reason': MISSING}
    with pytest.raises(PolicyError, match='execution stays disabled'):
        require('command')


def test_darwin_refuses_non_executable_or_symlinked_binary(monkeypatch, fake_sandbox, tmp_path):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    fake_sandbox.chmod(0o644)
    assert provider()['supported'] is False
    fake_sandbox.chmod(0o755)
    link = tmp_path / 'link'
    link.symlink_to(fake_sandbox)
    monkeypatch.setattr(platform_providers, 'SANDBOX_EXEC', str(link))
    assert provider()['supported'] is False
    directory = tmp_path / 'dir'
    directory.mkdir()
    monkeypatch.setattr(platform_providers, 'SANDBOX_EXEC', str(directory))
    assert provider()['supported'] is False


def test_unknown_kind_and_malformed_platform_refuse(monkeypatch, fake_sandbox):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    for kind in ('shell', 'Command', '', None, 'mcp_http'):
        with pytest.raises(PolicyError, match='Unknown execution kind'):
            require(kind)
    for platform in ('', 'x' * 65, 7):
        assert provider(platform) == {'platform': None, 'supported': False, 'confinement': None, 'reason': REASON}


def test_entries_are_fresh_copies(monkeypatch, fake_sandbox):
    monkeypatch.setattr(sys, 'platform', 'linux')
    entry = provider()
    entry['supported'] = True
    assert provider()['supported'] is False


@pytest.mark.skipif(sys.platform != 'darwin', reason='real-host probe only on macOS')
def test_this_mac_reports_the_real_binary():
    # The shipped confinement (E1/E9) runs /usr/bin/sandbox-exec, a regular system binary.
    assert provider() == {'platform': 'darwin', 'supported': True, 'confinement': 'sandbox-exec', 'reason': None}
