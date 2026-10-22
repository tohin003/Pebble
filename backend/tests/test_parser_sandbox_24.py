"""Phase 24 (t24-build): the confined parser's sandbox (V1-051) and its XML guard (V1-138).

The Mach lookups are measured, not read from the profile text: a Python process runs under the exact profile
documents.parse_document applies and asks launchd for each service with bootstrap_look_up(3). A name counts as
denied only when this unsandboxed test process can resolve it, so a missing service never passes vacuously. A
lookup sends no network traffic. The test whose name contains `real_sips` runs the real sips under the profile and
is the parent's to run.
"""
import json
import re
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from pebble import attachments, documents, exports
from pebble.attachments import XML_REFUSED, parse_file

pytestmark = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
                                reason='macOS sandbox-exec')

LOOKUP = r'''
import ctypes, json, sys
system = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
bootstrap = ctypes.c_uint.in_dll(system, 'bootstrap_port')
found = {}
for name in sys.argv[1:]:
    port = ctypes.c_uint(0)
    found[name] = system.bootstrap_look_up(bootstrap, name.encode(), ctypes.byref(port))
print(json.dumps(found))
'''
# URL loading, name resolution, credentials and keychain, LaunchServices, AppleEvents and configuration.
DENIED = ('com.apple.nsurlsessiond', 'com.apple.nsurlsessiond.NSURLSessionProxyService', 'com.apple.cfnetwork.AuthBroker',
          'com.apple.cfnetwork.cfnetworkagent', 'com.apple.networkd', 'com.apple.mDNSResponder', 'com.apple.dnssd.service',
          'com.apple.SecurityServer', 'com.apple.securityd', 'com.apple.securityd.xpc', 'com.apple.secd', 'com.apple.trustd',
          'com.apple.trustd.agent', 'com.apple.SystemConfiguration.configd',
          'com.apple.SystemConfiguration.DNSConfiguration', 'com.apple.coreservices.launchservicesd',
          'com.apple.CoreServices.coreservicesd', 'com.apple.coreservices.appleevents', 'com.apple.tccd',
          'com.apple.tccd.system', 'com.apple.windowserver.active',
          'com.apple.WebKit.Networking', 'com.apple.WebKit.WebContent')
FORBIDDEN_TEXT = ('nsurl', 'cfnetwork', 'networkd', 'mdnsresponder', 'dnssd', 'securityd', 'secd', 'securityserver',
                  'authbroker', 'webkit', 'trustd', 'configd', 'launchservicesd', 'coreservicesd', 'appleevents',
                  'tccd', 'keychain', 'windowserver')
# The parent's measurement of macOS's HEIF decoder under the image profile (2026-10-22): the minimal set, pinned.
IMAGE_SERVICES = ('com.apple.coremedia.videodecoder', 'com.apple.bsd.dirhelper')
IMAGE_LINES = (
    '(allow iokit-open (iokit-user-client-class "IOSurfaceRootUserClient"))',
    '(allow user-preference-read)',
    '(allow ipc-posix-shm-read-data ipc-posix-shm-read-metadata (ipc-posix-name-prefix "apple.cfprefs."))',
    r'(allow file-write* file-read* (regex #"^/private/var/folders/[^/]+/[^/]+/T/\.?[0-9A-F-]+(-[A-Za-z0-9]+)?$"))',
)


def lookups(names, profile=None):
    argv = [sys.executable, '-I', '-c', LOOKUP, *names]
    if profile is not None:
        argv = ['/usr/bin/sandbox-exec', '-p', profile, *argv]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


@pytest.fixture
def profile(tmp_path):
    target = tmp_path / 'input.png'
    target.write_bytes(b'\x89PNG\r\n\x1a\n')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    return documents.parser_profile(target.resolve(), scratch.resolve()), target.resolve(), scratch.resolve()


def test_the_profile_lists_its_mach_services_and_names_no_network_or_credential_service(profile):
    text = profile[0]  # an image parse: the widest profile the parser ever gets
    assert '(deny default)' in text and '(allow mach-lookup)' not in text and 'allow network' not in text
    granted = documents.PARSER_MACH_SERVICES + documents.IMAGE_MACH_SERVICES
    allowed = [line for line in text.splitlines() if 'mach-lookup' in line]
    assert len(allowed) == 1 and all('(global-name "%s")' % name in allowed[0] for name in granted)
    assert allowed[0].count('(global-name ') == len(granted)
    # Still refused, image grants included: coreservicesd, tccd, LaunchServices, network, DNS, keychain, AppleEvents.
    assert not [word for word in FORBIDDEN_TEXT if word in allowed[0].lower()]
    assert not [name for name in granted if any(word in name.lower() for word in FORBIDDEN_TEXT)]
    # Writes: /dev/null and this parse's scratch folder, plus ImageIO's temporary files (image parses only).
    writes = [line.strip() for line in text.splitlines() if 'file-write' in line]
    assert sorted(writes) == sorted(['(allow file-write* (subpath "%s") (literal "/dev/null"))' % profile[2],
                                     IMAGE_LINES[3]])
    assert documents.PARSER_IOKIT_CLASSES == ()


def test_image_parses_get_exactly_the_measured_heif_decoder_grants(profile):
    """V1-045 with V1-051: the parent's minimal measured set, by exact text, and nothing more."""
    assert documents.IMAGE_MACH_SERVICES == IMAGE_SERVICES
    assert documents.IMAGE_IOKIT_CLASSES == ('IOSurfaceRootUserClient',)
    lines = [line.strip() for line in profile[0].splitlines()]
    for rule in IMAGE_LINES:
        assert lines.count(rule) == 1, rule
    assert [line for line in lines if line.startswith('(allow iokit')] == [IMAGE_LINES[0]]
    for absent in ('AGXDeviceUserClient', 'IOSurfaceAcceleratorClient', 'user-preference-write', 'dtracehelper'):
        assert absent not in profile[0], absent
    # The temporary-file rule reaches only direct, hexadecimal-named children of the per-user Darwin temp folder.
    pattern = re.compile(IMAGE_LINES[3].split('#"', 1)[1].rsplit('"', 1)[0])
    for allowed in ('/private/var/folders/ab/xyz_123/T/0F3A2B4C-1D2E-4F50-8A9B-0C1D2E3F4A5B',
                    '/private/var/folders/ab/xyz_123/T/.0F3A2B4C', '/private/var/folders/ab/xyz_123/T/0F3A-Ab12'):
        assert pattern.fullmatch(allowed), allowed
    for refused in ('/private/var/folders/ab/xyz_123/T/pytest-of-someone/run1/out.txt',
                    '/private/var/folders/ab/xyz_123/T/0F3A/nested', '/private/var/folders/ab/xyz_123/C/0F3A',
                    '/private/var/folders/ab/xyz_123/T/report.json', '/private/tmp/0F3A', '/Users/someone/0F3A'):
        assert not pattern.fullmatch(refused), refused


def test_pdf_and_document_parses_get_none_of_the_image_grants(tmp_path):
    target = tmp_path / 'input.pdf'
    target.write_bytes(b'%PDF-1.4\n')
    for flag in ('--extract-pdf', '--extract-file'):
        text = documents.parser_command(target.resolve(), flag)[2]
        # (Not '/private/var/folders' itself: pytest's own temp folders may sit there and appear as path literals.)
        for word in IMAGE_SERVICES + ('iokit-open', 'IOSurfaceRootUserClient', 'user-preference-read', 'apple.cfprefs.',
                                      'regex'):
            assert word not in text, (flag, word)
        assert [line.strip() for line in text.splitlines() if 'file-write' in line] == [
            '(allow file-write* (literal "/dev/null"))']
        assert all('(global-name "%s")' % name in text for name in documents.PARSER_MACH_SERVICES)
        # Only an image parse may have a scratch folder (and with it the image grants).
        with pytest.raises(ValueError):
            documents.parser_command(target.resolve(), flag, tmp_path)
    assert documents.parser_profile(target.resolve()) == documents.parser_command(target.resolve(), '--extract-pdf')[2]


def test_the_parser_cannot_reach_url_dns_or_credential_services_and_keeps_what_python_needs(profile):
    granted = documents.PARSER_MACH_SERVICES + documents.IMAGE_MACH_SERVICES
    outside = lookups(DENIED + granted)
    inside = lookups(DENIED + granted, profile[0])
    reachable = [name for name in DENIED if outside[name] == 0]
    # URL loading, DNS (dnssd is mDNSResponder's service name on current macOS), the security server and configd
    # resolve on this Mac outside the sandbox, so the measurement is never vacuous.
    assert {'com.apple.nsurlsessiond', 'com.apple.cfnetwork.cfnetworkagent', 'com.apple.dnssd.service',
            'com.apple.SecurityServer', 'com.apple.SystemConfiguration.configd'} <= set(reachable), reachable
    assert {name: inside[name] for name in reachable if inside[name] == 0} == {}
    # Every granted service that exists here still resolves under the profile.
    assert {name: inside[name] for name in granted if outside[name] == 0 and inside[name]} == {}
    assert inside['com.apple.system.opendirectoryd.libinfo'] == 0


def test_the_parser_has_no_network_and_writes_only_its_scratch_folder(profile, tmp_path):
    text, target, scratch = profile
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    code = r'''
import json, socket, subprocess, sys
out = {}
try:
    socket.create_connection(('127.0.0.1', %d), timeout=2).close()
    out['connect'] = 'connected'
except OSError as error:
    out['connect'] = type(error).__name__
for name, path in (('scratch', %r), ('outside', %r)):
    try:
        open(path, 'w').write('x')
        out[name] = 'written'
    except OSError as error:
        out[name] = type(error).__name__
out['exec'] = subprocess.run(['/usr/bin/true']).returncode  # the parser may run a system tool (sips)
print(json.dumps(out))
''' % (port, str(scratch / 'decoded.png'), str(tmp_path / 'outside.txt'))
    try:
        done = subprocess.run(['/usr/bin/sandbox-exec', '-p', text, sys.executable, '-I', '-c', code],
                              capture_output=True, text=True, timeout=60)
    finally:
        listener.close()
    assert done.returncode == 0, done.stderr[-2000:]
    found = json.loads(done.stdout)
    assert found == {'connect': 'PermissionError', 'scratch': 'written', 'outside': 'PermissionError', 'exec': 0}
    assert (scratch / 'decoded.png').read_text() == 'x' and not (tmp_path / 'outside.txt').exists()


def test_image_parses_get_a_private_scratch_folder_that_is_removed_afterwards(tmp_path, monkeypatch):
    seen = {}
    real = documents.subprocess.run

    def record(cmd, **kwargs):
        seen['env'], seen['profile'] = kwargs.get('env'), cmd[2]
        seen['scratch'] = Path(kwargs['env'][documents.SCRATCH_ENV])
        seen['existed'] = seen['scratch'].is_dir()
        return real(cmd, **kwargs)
    monkeypatch.setattr(documents.subprocess, 'run', record)
    from PIL import Image
    image = tmp_path / 'shot.png'
    Image.new('RGB', (20, 10), '#123456').save(image)
    assert json.loads(documents.parse_document(image, '--extract-image'))['width'] == 20
    assert seen['existed'] and not seen['scratch'].exists() and str(seen['scratch']) in seen['profile']
    assert seen['env']['TMPDIR'] == str(seen['scratch']) + '/'
    seen.clear()
    text = tmp_path / 'notes.txt'
    text.write_text('hello')
    monkeypatch.setattr(documents.subprocess, 'run', lambda cmd, **kwargs: seen.update(env=kwargs.get('env')) or real(cmd, **kwargs))
    documents.parse_document(text, '--extract-file')
    assert seen['env'] is None  # file parses get no writable folder at all


def test_exports_deny_name_resolution_to_textutil():
    for name in ('com.apple.mDNSResponder', 'com.apple.dnssd.service', 'com.apple.SecurityServer'):
        assert '(global-name "%s")' % name in exports._NO_SERVICES
    assert exports._NO_SERVICES.startswith('(deny mach-lookup') and exports._NO_SERVICES in exports.TEXTUTIL_PROFILE


# V1-138: office XML with a DOCTYPE or ENTITY declaration is refused before any expansion -----------------------

BOMB = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><document><p>&c;</p></document>')


def office(path, entry, xml, encoding='utf-8', extra=()):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(entry, xml.encode(encoding) if isinstance(xml, str) else xml)
        for name, data in extra:
            archive.writestr(name, data)
    return path


@pytest.mark.parametrize('name,entry,encoding,extra', [
    ('bomb.docx', 'word/document.xml', 'utf-8', ()),
    ('bomb16.docx', 'word/document.xml', 'utf-16', ()),  # the byte check alone would miss this one
    ('strings.xlsx', 'xl/sharedStrings.xml', 'utf-8', (('xl/workbook.xml', '<workbook/>'),)),
    ('content.odt', 'content.xml', 'utf-16', ()),
])
def test_office_xml_with_entity_declarations_is_refused(tmp_path, name, entry, encoding, extra):
    xml = BOMB if encoding == 'utf-8' else BOMB.replace('<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-16"?>')
    path = office(tmp_path / name, entry, xml, encoding, extra)
    with pytest.raises(ValueError) as error:
        parse_file(path)
    assert str(error.value) == XML_REFUSED


def test_a_plain_doctype_is_refused_and_ordinary_office_xml_still_reads(tmp_path):
    with pytest.raises(ValueError, match='DOCTYPE'):
        attachments.safe_xml('<!DOCTYPE html><document/>'.encode('utf-16'))
    assert attachments.safe_xml('<document><p>fine</p></document>'.encode('utf-16')).tag == 'document'
    path = office(tmp_path / 'ok.docx', 'word/document.xml', '<document><p><r><t>Plain text</t></r></p></document>')
    assert parse_file(path)['text'] == 'Plain text'


def test_the_confined_worker_returns_the_refusal_through_attach(tmp_path):
    from pebble.app import App
    app = App(tmp_path / 'data', Path('src-tauri/resources'), lambda *a: None)
    try:
        path = office(tmp_path / 'bomb.docx', 'word/document.xml', BOMB)
        with pytest.raises(ValueError) as error:
            app.attach(app.new_chat()['id'], str(path))
        assert str(error.value) == XML_REFUSED
    finally:
        app.close()


def test_the_export_readback_worker_refuses_declarations_in_any_encoding():
    from pebble import export_worker
    for data in (BOMB.encode(), BOMB.replace('<?xml version="1.0"?>', '').encode('utf-16')):
        with pytest.raises(export_worker.ReadbackError):
            export_worker._xml(data)
    assert export_worker._xml('<w><t>ok</t></w>'.encode('utf-16')).tag == 'w'


def test_sips_decodes_under_the_parser_profile_real_sips(tmp_path):
    """The parent's measurement of V1-051 for sips: decode a PNG-made HEIC with the real sips under the profile."""
    from PIL import Image
    original, heic = tmp_path / 'o.png', tmp_path / 'p.heic'
    Image.new('RGB', (48, 32), '#587958').save(original)
    subprocess.run(['/usr/bin/sips', '-s', 'format', 'heic', str(original), '--out', str(heic)], check=True,
                   capture_output=True)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    text = documents.parser_profile(heic.resolve(), scratch.resolve())
    done = subprocess.run(['/usr/bin/sandbox-exec', '-p', text, '/usr/bin/sips', '-s', 'format', 'png', str(heic.resolve()),
                           '--out', str(scratch.resolve() / 'decoded.png')],
                          capture_output=True, text=True, timeout=60,
                          env={'PATH': '/usr/bin:/bin', 'HOME': str(scratch), 'TMPDIR': str(scratch) + '/', 'LANG': 'C'})
    assert done.returncode == 0, done.stderr[-2000:]
    with Image.open(scratch / 'decoded.png') as decoded:
        assert decoded.size == (48, 32)
