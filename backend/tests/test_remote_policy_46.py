"""Phase 46 (1.9.29, D214, D215): Pebble Remote's tables, keys, QR code and folders, with no phone and no network.

- The phone's allowlist is ALLOWED - PHONE_EXCLUDED, with ALLOWED the copy of main.rs's list: these tests fail when
  the copy and main.rs differ, when Pebble's setting() or settings_file gains a key the bridge has not classified, and
  when a widening key is not elevated.
- The QR encoder is checked against the standard's known values (Reed-Solomon, format and version bits, capacities)
  and read back by an independent decoder.
- Zero cost while Remote is off: no thread, no socket, no file, and a tee measured per event.
"""

import ast
import os
import re
import stat
import threading
import time
from pathlib import Path

import pytest

from pebble import companion_settings, remote_keys, remote_mac, remote_policy as policy, remote_qr, settings_file
from pebble import tool_authz

ROOT = Path(__file__).resolve().parents[2]
PEBBLE = ROOT / 'backend/pebble'
MAIN_RS = ROOT / 'src-tauri/src/main.rs'


def rust_list(pattern):
    return re.findall(r'"([a-z_]+)"', re.search(pattern, MAIN_RS.read_text(encoding='utf-8'), re.S).group(1))


# ---- the allowlist (3.7) ----------------------------------------------------------------------------------------------

def test_allowed_is_main_rs_list_exactly_and_native_only_is_its_shell_list():
    assert list(policy.ALLOWED) == rust_list(r'const ALLOWED: &\[&str\] = &\[(.*?)\];'), \
        'remote_policy.ALLOWED must be main.rs ALLOWED, in its order'
    assert set(policy.SHELL_NATIVE_ONLY) == set(rust_list(r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];'))
    assert len(set(policy.ALLOWED)) == len(policy.ALLOWED)


def test_pebble_remotes_own_routes_are_the_main_windows_and_never_the_phones_or_a_models():
    pet = set(rust_list(r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains'))
    assert set(policy.MAC_ROUTES) <= set(policy.ALLOWED) and not set(policy.MAC_ROUTES) & pet
    assert not set(policy.MAC_ROUTES) & policy.METHOD_SET, 'a phone never pairs, confirms or removes'
    assert set(policy.MAC_ROUTES) <= tool_authz.NATIVE_ONLY
    from pebble.remote import RemoteHub
    hub = RemoteHub()
    assert all(hub.owns(route) and callable(getattr(hub, route)) for route in policy.MAC_ROUTES)
    assert not any(hub.owns(method) for method in policy.ALLOWED if method not in policy.MAC_ROUTES)


def test_the_phone_gets_allowed_minus_the_contracts_exclusions():
    excluded_v03 = {
        'attach', 'model_save', 'model_save_many', 'model_remove', 'model_discover', 'set_model', 'memory_export',
        'memory_archive_export_to', 'memory_archive_preview_file', 'memory_archive_import_file', 'export_save',
        'git_export', 'git_export_file', 'media_download', 'telemetry_export', 'telemetry_clear', 'storage_reveal',
        'storage_set', 'storage_settings_resolve', 'storage_forget_resolve', 'storage_cleanup_legacy',
        'storage_delete', 'chatgpt_start', 'chatgpt_cancel', 'chatgpt_sign_out', 'plan_credentials',
        'plan_credential_save', 'plan_credential_delete', 'mod_install', 'mod_example', 'mod_preview', 'mod_remove',
        'extension_add', 'extension_import', 'extension_remove', 'browser_connect_install', 'upgrade_backups',
        'upgrade_discard_backup', 'upgrade_retry', 'browser_profile_forget', 'browser_extension_unpair'}
    offered = set(policy.PHONE_ALLOWED)
    assert not offered & excluded_v03
    assert not offered & policy.SHELL_NATIVE_ONLY
    assert not [m for m in offered if m.startswith('plugin_package_')]
    assert [m for m in offered if m.startswith('connector_')] == ['connector_status']
    assert set(policy.OPEN_ON_MAC) <= offered, 'v0.5 item 3: opening things on the Mac is allowed (elevated)'
    assert set(policy.ALLOWED) - offered == {m for m in policy.ALLOWED if policy.phone_excluded(m)}
    assert {'state', 'get_chat', 'new_chat', 'send', 'cancel', 'setting', 'approve', 'set_workspace'} <= offered
    assert list(policy.METHODS[-len(policy.REMOTE_METHODS):]) == list(policy.REMOTE_METHODS)


def test_every_method_the_phone_is_offered_is_one_pebble_answers(tmp_path):
    from pebble.app import App
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *a: None)
    try:
        routes = set(app._route_table())
    finally:
        app.close()
    missing = sorted(m for m in policy.PHONE_ALLOWED if m not in routes)
    assert missing == []
    assert policy.SURFACE_ROUTES <= routes


def test_elevated_names_every_proof_method_offered_and_the_widening_setting_keys():
    elevated = set(policy.ELEVATED)
    assert {m for m in policy.METHODS if m in policy.ELEVATED_ALWAYS} == {e for e in elevated if ':' not in e}
    assert {'remote.unlock', 'approve', 'approve_simple', 'setting:permissions'} <= elevated
    assert set(policy.OPEN_ON_MAC) <= elevated
    assert all(e in policy.METHOD_SET or e.startswith('setting:') for e in elevated)
    assert not elevated & policy.UNLOCK_FREE and 'remote.lock' in policy.UNLOCK_FREE
    assert policy.UNLOCK_FREE <= policy.READ_ONLY


def _setting_keys():
    tree = ast.parse((PEBBLE / 'app.py').read_text(encoding='utf-8'))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'setting')
    keys = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'key':
            for item in node.comparators:
                if isinstance(item, ast.Constant):
                    keys.add(item.value)
                elif isinstance(item, (ast.Set, ast.Tuple, ast.List)):
                    keys |= {e.value for e in item.elts if isinstance(e, ast.Constant)}
                elif isinstance(item, ast.Attribute) and item.attr == 'KEY':
                    keys.add(companion_settings.KEY)
    return keys


def _widening_keys():
    tree = ast.parse((PEBBLE / 'settings_file.py').read_text(encoding='utf-8'))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'widening')
    keys = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'key':
            for item in node.comparators:
                keys.add(item.value if isinstance(item, ast.Constant) else getattr(settings_file, item.id))
    return keys


def test_every_setting_key_pebble_accepts_is_classified_and_every_widening_key_needs_a_proof():
    accepted = _setting_keys()
    assert {'permissions', 'theme', 'companion', 'notifications', 'fast_replies', 'git_decisions'} <= accepted
    unclassified = sorted((accepted | set(settings_file.KEYS)) - set(policy.SETTING_CLASSES))
    assert unclassified == [], 'classify these setting keys in remote_policy.SETTING_CLASSES'
    widening = _widening_keys()
    assert {'permissions', 'memory_enabled', 'memory_layers', 'notifications', 'storage', 'git_decisions'} <= widening
    assert all(policy.SETTING_CLASSES[key] == 'elevated' for key in widening)


def test_a_setting_key_cannot_dodge_its_proof():
    known = lambda: frozenset({'/Users/me/p'})
    for params in ({'key': 'permissions'}, {'key': ['permissions']}, {'key': 'Permissions'}, {'key': 'permissions '},
                   {'key': None}, {}, {'key': 'not_a_setting'}, {'key': 'storage'}):
        assert policy.needs_proof('setting', params, known, 'd-1'), params
    # Restated (w27's review F10): Stealth Lockdown silences the Mac's notices of what a phone does next, so it left
    # this list of keys a phone changes with the unlock alone; it needs Face ID now, as the widening keys do.
    for key in ('theme', 'fast_replies', 'reduced_motion', 'companion'):
        assert not policy.needs_proof('setting', {'key': key}, known, 'd-1')
    assert policy.needs_proof('setting', {'key': 'stealth', 'value': True}, known, 'd-1')
    assert 'setting:stealth' in policy.ELEVATED


def test_new_folders_and_other_phones_need_a_proof_known_projects_and_the_companion_do_not():
    known = lambda: frozenset({'/Users/me/p'})
    for method, field in (('set_workspace', 'path'), ('new_chat', 'workspace')):
        assert not policy.needs_proof(method, {field: None}, known, 'd-1')
        assert not policy.needs_proof(method, {field: ''}, known, 'd-1')
        assert not policy.needs_proof(method, {field: '/Users/me/p'}, known, 'd-1')
        for value in ('/Users/me/new', '/Users/me/p/', 5, ['/Users/me/p'], {'x': 1}):
            assert policy.needs_proof(method, {field: value}, known, 'd-1'), (method, value)
    assert not policy.needs_proof('new_chat', {}, known, 'd-1')
    assert not policy.needs_proof('remote.revoke_device', {'device_id': 'd-1'}, known, 'd-1')
    assert policy.needs_proof('remote.revoke_device', {'device_id': 'd-2'}, known, 'd-1')
    assert policy.needs_proof('approve', {}, known, 'd-1') and not policy.needs_proof('send', {}, known, 'd-1')


def test_request_ids_events_and_the_activity_words():
    assert policy.valid_id('Az09_-' + 'y' * 58) and not policy.valid_id('x' * 65)
    for bad in ('has space', 'dot.ted', 'line\nbreak', '', None, 5):
        assert not policy.valid_id(bad)
    assert policy.forwarded('token') and policy.forwarded('activity') and policy.forwarded('settings')
    assert not policy.forwarded('native_export_pdf') and not policy.forwarded('remote_state')
    assert policy.done_words('state', {}) is None and policy.done_words('remote.unlock', {}) is None
    assert policy.done_words('approve', {}) == 'approved'
    assert policy.done_words('setting', {'key': 'permissions'}) == 'changed Permissions'
    assert policy.done_words('remote.screenshot', {}) == 'took a screenshot of this Mac'
    assert policy.done_words('goal_create', {}) == 'changed a goal'
    words = ' '.join(list(policy.DONE.values()) + [w for _, w in policy.AREAS])
    assert not re.search(r'\b(inference|receipts?|host|runtime|capacity)\b', words, re.I)


# ---- keys and transcripts (3.1 to 3.6, X-1) -----------------------------------------------------------------------

def test_base64url_is_strict():
    assert remote_keys.b64u_decode('AQID') == b'\x01\x02\x03' and remote_keys.b64u_decode('AQI') == b'\x01\x02'
    for bad in ('AQID=', 'AQ+D', 'A', 'AQ/D', ' AQID', 5, None):
        with pytest.raises(ValueError):
            remote_keys.b64u_decode(bad)
    with pytest.raises(ValueError):
        remote_keys.b64u_decode('AR')  # non-zero padding bits: one value, one spelling


def test_transcripts_and_the_six_digits_match_the_contracts_golden_values():
    # T_proof v0.3 golden (bridge-mock README, agreed with the phone's lane at 01:16 and 01:22)
    text = remote_keys.t_proof('b-1', 'd-1', 'e-1', 'n-1', 'r42', '0f8fad5b-d9cb-469f-a165-70867728950e', 'approve',
                               '{"action_id":"a9","id":"c1"}', 1761780000000)
    assert text.split(b'\n')[8] == b'IjGkhgxEHL96SaH_YpvZ_QIRTIdaiepSsS2nStVZGXM'
    import hashlib
    assert hashlib.sha256(text).hexdigest() == 'd0e32872b7515632da3ffaa1059db00ad0ba78c80297ed5c495492479d70a19e'
    # X-1's golden vectors (the first keeps its leading zero)
    assert remote_keys.sas_code(remote_keys.t_pair('b-1', 'n-1', 'p-1', 'c-1', 'dk-1', 'ak-1')) == '035072'
    second = ('b-AAECAwQFBgcICQoL', 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8',
              'uBxfZqYw5vyQji5sLR4Ex6QEFGKWKNk-v7nH6PPPyFM', 'ZGVmZ2hpamtsbW5vcHFycw',
              'BAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4fICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8',
              'BEBBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5fYGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6e3x9fn8')
    assert remote_keys.sas_code(remote_keys.t_pair(*second)) == '994460'
    with pytest.raises(ValueError):
        remote_keys.t_hello('b-1', 'd-1\nforged', 'n-1', 'p-1')


def test_the_tls_identity_is_owner_only_tls13_and_keeps_its_pin(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    import hashlib
    import ssl
    folder = tmp_path / 'remote'
    identity = remote_keys.Identity(folder)
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    for name in ('tls-key.pem', 'tls-cert.pem'):
        assert stat.S_IMODE(os.stat(folder / name).st_mode) == 0o600
    cert = x509.load_pem_x509_certificate((folder / 'tls-cert.pem').read_bytes())
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert identity.pin == remote_keys.b64u(hashlib.sha256(spki).digest())
    assert cert.public_key().curve.name == 'secp256r1'
    (folder / 'tls-cert.pem').unlink()  # a certificate is made again with the same key: the pin stays
    assert remote_keys.Identity(folder).pin == identity.pin
    context = identity.tls_context()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3 and context.maximum_version == ssl.TLSVersion.TLSv1_3
    assert context.options & ssl.OP_NO_TICKET


def test_phone_signatures_are_checked_strictly():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
    from cryptography.hazmat.primitives import serialization
    private = ec.generate_private_key(ec.SECP256R1())
    point = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    key = remote_keys.phone_key(remote_keys.b64u(point))
    signature = private.sign(b'message', ec.ECDSA(hashes.SHA256()))
    assert remote_keys.verify(key, b'message', signature) and not remote_keys.verify(key, b'other', signature)
    r, s = decode_dss_signature(signature)
    twin = encode_dss_signature(r, 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551 - s)
    assert remote_keys.verify(key, b'message', twin), "the (r, n-s) twin verifies: C2's memory is keyed by r"
    assert remote_keys.signature_r(twin) == remote_keys.signature_r(signature) == r
    assert not remote_keys.verify(key, b'message', signature + b'\x00'), 'nothing after the DER sequence'
    assert not remote_keys.verify(key, b'message', b'\x30\x00')
    for bad in ('', remote_keys.b64u(b'\x04' + b'\x00' * 64), remote_keys.b64u(point[:64]), remote_keys.b64u(
            b'\x02' + point[1:33]), 'not base64!'):
        with pytest.raises(ValueError):
            remote_keys.phone_key(bad)


def test_the_pairing_link_is_percent_encoded():
    link = remote_keys.pairing_link('b-AAAA', 'Sam’s Mac & co', 'pin', 'code', ['192.168.1.2:47800', '[fe80::1]:1'])
    assert link.startswith('pebble-remote://pair?v=1&bid=b-AAAA&name=Sam%E2%80%99s%20Mac%20%26%20co&pin=pin&code=code')
    assert link.endswith('&ep=192.168.1.2%3A47800&ep=%5Bfe80%3A%3A1%5D%3A1')


# ---- the QR code --------------------------------------------------------------------------------------------------

def test_reed_solomon_format_and_version_bits_match_the_standards_known_values():
    # "HELLO WORLD", version 1-M: the standard's worked example of the 10 error correction codewords
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    assert remote_qr.rs_remainder(data, remote_qr.rs_divisor(10)) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
    table = {'L': (0x77C4, 0x72F3, 0x7DAA, 0x789D, 0x662F, 0x6318, 0x6C41, 0x6976),
             'M': (0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0),
             'Q': (0x355F, 0x3068, 0x3F31, 0x3A06, 0x24B4, 0x2183, 0x2EDA, 0x2BED),
             'H': (0x1689, 0x13BE, 0x1CE7, 0x19D0, 0x0762, 0x0255, 0x0D0C, 0x083B)}
    for level, words in table.items():
        assert [remote_qr.format_bits(level, mask) for mask in range(8)] == list(words), level
    assert [remote_qr.version_bits(v) for v in (7, 8, 9, 10, 40)] == [0x07C94, 0x085BC, 0x09A99, 0x0A4D3, 0x28C69]


def test_the_tables_give_the_standards_byte_capacities():
    capacity = {1: (17, 14, 11, 7), 2: (32, 26, 20, 14), 3: (53, 42, 32, 24), 4: (78, 62, 46, 34),
                5: (106, 84, 60, 44), 6: (134, 106, 74, 58), 7: (154, 122, 86, 64), 8: (192, 152, 108, 84),
                9: (230, 180, 130, 98), 10: (271, 213, 151, 119), 12: (367, 287, 203, 155), 13: (425, 331, 241, 177),
                20: (858, 666, 482, 382), 40: (2953, 2331, 1663, 1273)}
    for version, expected in capacity.items():
        overhead = 12 if version < 10 else 20
        got = tuple((remote_qr.data_codewords(version, level) * 8 - overhead) // 8 for level in 'LMQH')
        assert got == expected, version


# Alignment pattern centres, from the standard's annex E table (not from the encoder's formula).
ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
             9: [6, 26, 46], 10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62], 14: [6, 26, 46, 66],
             15: [6, 26, 48, 70], 20: [6, 34, 62, 90], 32: [6, 34, 60, 86, 112, 138]}


def test_alignment_positions_are_the_standards():
    for version, centres in ALIGNMENT.items():
        assert remote_qr.alignment_positions(version) == centres, version


def _function_modules(version):
    """Function patterns per the standard, drawn here independently of the encoder."""
    size = version * 4 + 17
    fixed = [[False] * size for _ in range(size)]

    def mark(x0, y0, w, h):
        for y in range(max(0, y0), min(size, y0 + h)):
            for x in range(max(0, x0), min(size, x0 + w)):
                fixed[y][x] = True
    mark(0, 0, 9, 9)
    mark(size - 8, 0, 8, 9)
    mark(0, size - 8, 9, 8)
    mark(6, 0, 1, size)
    mark(0, 6, size, 1)
    centers = ALIGNMENT[version]
    for cx in centers:
        for cy in centers:
            if (cx, cy) not in ((6, 6), (6, centers[-1]), (centers[-1], 6)):
                mark(cx - 2, cy - 2, 5, 5)
    if version >= 7:
        mark(size - 11, 0, 3, 6)
        mark(0, size - 11, 6, 3)
    return fixed


def _gf():
    exp, log, value = [0] * 512, [0] * 256, 1
    for i in range(255):
        exp[i], log[value] = value, i
        value = (value << 1) ^ (0x11D if value & 0x80 else 0)
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


def decode(rows):
    """Read a QR code back: format bits, unmasking, the codewords in their zigzag, blocks with their Reed-Solomon
    syndromes all zero, then the byte-mode text."""
    size, exp, log = len(rows), *_gf()
    version = (size - 17) // 4
    dark = lambda x, y: rows[y][x] == '1'
    first = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    second = [(size - 1 - i, 8) for i in range(8)] + [(8, size - 15 + i) for i in range(8, 15)]
    raw = sum(dark(x, y) << i for i, (x, y) in enumerate(first))
    assert raw == sum(dark(x, y) << i for i, (x, y) in enumerate(second)), 'the two format copies agree'
    level, mask = next((lv, m) for lv in 'LMQH' for m in range(8) if remote_qr.format_bits(lv, m) == raw)
    assert dark(8, size - 8), 'the dark module'
    rule = remote_qr.MASKS[mask]
    fixed = _function_modules(version)
    bits, x, upward = [], size - 1, True
    while x > 0:
        if x == 6:
            x -= 1
        for y in (range(size - 1, -1, -1) if upward else range(size)):
            for dx in (0, 1):
                if not fixed[y][x - dx]:
                    bits.append(int(dark(x - dx, y)) ^ int(rule(x - dx, y)))
        upward, x = not upward, x - 2
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits) - 7, 8)]
    total = remote_qr.raw_modules(version) // 8
    words = words[:total]
    blocks, ecc = remote_qr.BLOCKS[level][version - 1], remote_qr.ECC_PER_BLOCK[level][version - 1]
    short = total // blocks - ecc
    count_short = blocks - total % blocks
    sizes = [short + (0 if i < count_short else 1) for i in range(blocks)]
    data, it = [[] for _ in range(blocks)], iter(words)
    for i in range(max(sizes)):
        for b in range(blocks):
            if i < sizes[b]:
                data[b].append(next(it))
    correction = [[] for _ in range(blocks)]
    for _ in range(ecc):  # interleaved too, one codeword of each block in turn
        for b in range(blocks):
            correction[b].append(next(it))
    for block, checks in zip(data, correction):
        message = block + checks
        for j in range(ecc):
            syndrome = 0
            for word in message:
                syndrome = (exp[(log[syndrome] + j) % 255] if syndrome else 0) ^ word
            assert syndrome == 0, 'a Reed-Solomon syndrome is not zero'
    stream = ''.join(format(w, '08b') for block in data for w in block)
    assert stream[:4] == '0100', 'byte mode'
    width = 8 if version < 10 else 16
    length = int(stream[4:4 + width], 2)
    body = stream[4 + width:4 + width + 8 * length]
    return bytes(int(body[i:i + 8], 2) for i in range(0, len(body), 8)).decode('utf-8'), version, level


@pytest.mark.parametrize('text,level', [
    ('HELLO WORLD', 'M'), ('x' * 14, 'M'), ('x' * 15, 'M'), ('pebble-remote://pair?v=1&bid=b-AAAAAAAAAAAAAAAA', 'L'),
    ('é' * 90, 'Q'), ('a' * 140, 'H'),
    ('pebble-remote://pair?v=1&bid=b-0123456789abcdef&name=Sam%E2%80%99s%20MacBook%20Pro&pin=' + 'P' * 43 + '&code='
     + 'C' * 22 + '&ep=192.168.1.20%3A47800&ep=Sams-MacBook-Pro.local%3A47800&ep=100.101.102.103%3A47800'
     '&ep=sams-macbook-pro.tail1234.ts.net%3A47800', 'M')])
def test_a_qr_code_reads_back_to_its_text(text, level):
    rows = remote_qr.encode(text, level)
    assert all(len(row) == len(rows) and set(row) <= {'0', '1'} for row in rows)
    got, version, read_level = decode(rows)
    assert (got, read_level) == (text, level)
    if text == 'x' * 14:
        assert version == 1
    if text == 'x' * 15:
        assert version == 2


def test_a_pairing_links_qr_code_is_small_enough_to_scan_and_costs_little():
    link = 'pebble-remote://pair?v=1&bid=b-' + 'A' * 16 + '&name=' + 'M' * 40 + '&pin=' + 'P' * 43 + '&code=' + \
           'C' * 22 + '&ep=192.168.100.200%3A47800' * 2 + '&ep=sams-macbook-pro.tail1234.ts.net%3A47800'
    started = time.perf_counter()
    rows = remote_qr.encode(link)
    took = time.perf_counter() - started
    assert len(rows) <= 73, 'version 14 or less'
    assert took < 2.0, took
    with pytest.raises(ValueError):
        remote_qr.encode('x' * 3000, 'H')


# ---- folders (v0.5) -----------------------------------------------------------------------------------------------

def test_folders_lists_subfolders_only_hides_hidden_and_protected_and_refuses_the_rest(tmp_path):
    home = tmp_path / 'home'
    for name in ('apps', 'Beta', 'alpha', '.ssh', '.config', 'PebbleBackups', 'secrets', 'keys.key', 'Documents'):
        (home / name).mkdir(parents=True)
    (home / 'Documents' / 'Pebble').mkdir()
    (home / 'apps' / 'web' / '.git').mkdir(parents=True)
    (home / 'apps' / 'tree').mkdir()
    (home / 'apps' / 'tree' / '.git').write_text('gitdir: elsewhere')
    (home / 'file.txt').write_text('never listed')
    os.symlink(home / 'apps', home / 'link-in')
    os.symlink('/usr', home / 'link-out')
    folders = remote_mac.Folders(home)
    real = os.path.realpath(home)
    listing = folders.listing()
    assert listing['home'] == real and listing['path'] == real and listing['parent'] is None
    assert [row['name'] for row in listing['folders']] == ['alpha', 'apps', 'Beta', 'Documents']
    assert all(set(row) == {'name', 'path', 'is_project'} for row in listing['folders'])
    inner = folders.listing(real + '/apps')
    assert inner['parent'] == real
    assert {row['name']: row['is_project'] for row in inner['folders']} == {'tree': True, 'web': True}
    assert folders.listing(real + '/Documents')['folders'] == [], 'Pebble\'s Documents copies are protected'
    for path, code in ((real + '/..', 'not_allowed'), ('/usr', 'not_allowed'), ('/', 'not_allowed'),
                       (real + '/link-in', 'not_allowed'), (real + '/link-out', 'not_allowed'),
                       (real + '/link-out/bin', 'not_allowed'), (real + '//apps', 'not_allowed'),
                       (real + '/.ssh', 'not_allowed'), (real + '/.ssh/nothing', 'not_allowed'),
                       (real + '/secrets', 'not_allowed'), (real + '/keys.key', 'not_allowed'),
                       (real + '/PebbleBackups', 'not_allowed'), (real + '/.' + 'claude-' + 'x', 'not_allowed'),
                       ('relative', 'bad_request'), ('', 'bad_request'), (5, 'bad_request'), ('/a\x00b', 'bad_request')):
        with pytest.raises(remote_mac.FolderRefused) as refused:
            folders.listing(path)
        assert refused.value.code == code, path
    with pytest.raises(remote_mac.FolderRefused) as refused:
        folders.listing(real + '/missing')
    assert refused.value.code is None and refused.value.words == 'Choose a folder that exists.'
    assert folders.listable(real + '/apps/web') and folders.listable(real + '/apps/')
    assert not folders.listable(real) and not folders.listable(real + '/link-in') and not folders.listable(None)


def test_folders_stops_at_500_and_lists_in_name_order(tmp_path):
    home = tmp_path / 'many'
    for index in range(520):
        (home / ('f%04d' % (519 - index))).mkdir(parents=True)
    rows = remote_mac.Folders(home).listing()['folders']
    assert len(rows) == 500 and rows[0]['name'] == 'f0000' and rows[-1]['name'] == 'f0499'


# ---- the Mac's helpers ----------------------------------------------------------------------------------------------

def test_lan_listening_takes_private_addresses_of_broadcast_interfaces_and_tailscales_tunnel_only():
    up, broadcast, running, loopback, p2p = 0x1, 0x2, 0x40, 0x8, 0x10
    found = [('lo0', up | loopback | running, '127.0.0.1'), ('en0', up | broadcast | running, '192.168.1.20'),
             ('en1', up | broadcast | running, '17.5.5.5'), ('en5', up | broadcast, '10.0.0.9'),
             ('utun3', up | p2p | running, '10.8.0.2'), ('utun4', up | p2p | running, '100.101.102.103'),
             ('bridge0', up | broadcast | running, '172.20.0.1'), ('en2', broadcast | running, '192.168.9.9')]
    assert remote_mac.address_plan(found) == (['172.20.0.1', '192.168.1.20'], ['100.101.102.103'])
    lan, tailnet = remote_mac.address_plan()  # this Mac, read with getifaddrs: whatever it is, never public
    import ipaddress
    assert all(any(ipaddress.ip_address(a) in net for net in remote_mac.PRIVATE) for a in lan)
    assert all(ipaddress.ip_address(a) in remote_mac.TAILNET for a in tailnet)


def test_the_battery_reads_pmset_and_a_mac_without_one_is_on_power(monkeypatch):
    monkeypatch.setattr(remote_mac, '_run', lambda argv, timeout=5: "Now drawing from 'Battery Power'\n "
                        "-InternalBattery-0 (id=4653155)\t64%; discharging; 4:12 remaining present: true")
    assert remote_mac.battery() == {'battery_percent': 64, 'charging': False, 'on_power': False}
    monkeypatch.setattr(remote_mac, '_run', lambda argv, timeout=5: "Now drawing from 'AC Power'\n "
                        "-InternalBattery-0 (id=1)\t87%; charging; 0:40 remaining present: true")
    assert remote_mac.battery() == {'battery_percent': 87, 'charging': True, 'on_power': True}
    monkeypatch.setattr(remote_mac, '_run', lambda argv, timeout=5: "Now drawing from 'AC Power'")
    assert remote_mac.battery() == {'battery_percent': None, 'charging': False, 'on_power': True}


def test_a_screenshot_is_scaled_jpeg_and_leaves_nothing_behind(tmp_path, monkeypatch):
    from remote_kit import fake_capture, jpeg_size
    made = []
    real = remote_mac.tempfile.mkdtemp
    monkeypatch.setattr(remote_mac.tempfile, 'mkdtemp', lambda **kw: made.append(real(dir=tmp_path, **kw)) or made[-1])
    shots = remote_mac.screenshot(None, capture=fake_capture, allowed=True)
    assert [(i, w, h) for i, w, h, _ in shots] == [(0, 2560, 1663), (1, 1920, 1080)]
    assert all(jpeg_size(data) == (w, h) for _, w, h, data in shots)
    assert [shot[0] for shot in remote_mac.screenshot(1, capture=fake_capture, allowed=True)] == [1]
    with pytest.raises(remote_mac.ScreenRefused, match='no display 5'):
        remote_mac.screenshot(5, capture=fake_capture, allowed=True)
    with pytest.raises(remote_mac.ScreenRefused, match='Screen & System Audio Recording'):
        remote_mac.screenshot(None, capture=fake_capture, allowed=False)
    assert len(made) == 3 and not any(os.path.exists(path) for path in made), 'every capture folder is gone'


def test_the_bonjour_watchdog_quotes_nothing_it_is_given():
    assert '"$2"' in remote_mac.BONJOUR_WATCH and '"$3"' in remote_mac.BONJOUR_WATCH and '"$1"' in remote_mac.BONJOUR_WATCH
    assert remote_mac.SERVICE == '_pebble-remote._tcp' and 'v=1 "bid=$2"' in remote_mac.BONJOUR_WATCH


# ---- zero cost while Remote is off (D213: nothing a benchmark run would feel) --------------------------------------------

def test_remote_off_holds_no_thread_socket_or_file_and_its_tee_is_measured(tmp_path):
    from pebble.remote import RemoteHub
    before = {t.name for t in threading.enumerate()}
    hub = RemoteHub()
    hub.attach(object(), tmp_path / 'data', lambda *a: None)
    assert not (tmp_path / 'data' / 'remote').exists(), 'Remote off writes nothing'
    assert hub.bridge is None and hub.status()['enabled'] is False
    data = {'chat_id': 'c' * 32, 'id': 'm' * 32, 'text': 'tok'}
    count = 200_000
    started = time.perf_counter()
    for _ in range(count):
        hub.tee('token', data)
    per_event_us = (time.perf_counter() - started) / count * 1e6
    # A model step in a benchmark run emits at most a few hundred events; 1,000 is the bound used here.
    per_step_ms = per_event_us * 1000 / 1000  # 1,000 events, microseconds to milliseconds
    print('remote off: %.3f us per event, %.4f ms per 1,000-event step' % (per_event_us, per_step_ms))
    assert per_event_us < 2.0, per_event_us
    assert per_step_ms < 2.0
    assert {t.name for t in threading.enumerate()} - before == set(), 'no thread while Remote is off'
    hub.close()


def test_main_starts_the_hub_beside_the_app_and_tees_every_event():
    source = (ROOT / 'backend/main.py').read_text(encoding='utf-8')
    assert 'remote = RemoteHub()' in source and 'remote.tee(name, data)' in source
    assert 'remote.attach(app, home, emit)' in source and 'remote.close()' in source
    assert source.index('remote.close()') < source.index('app.close()'), 'the phones go before the store does'
    assert 'remote.dispatch(method, params) if remote.owns(method) else app.dispatch(method, params)' in source
