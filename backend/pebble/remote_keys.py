"""Phase 46 (1.9.29, D214): the iPhone bridge's keys and the texts both sides sign (PEBBLE_REMOTE_BRIDGE 3.1 to 3.6).

- The bridge's TLS identity: an ECDSA P-256 key made with `cryptography`, kept owner-only in Pebble's data folder as
  Pebble's other keys are, and a self-signed certificate re-issued with the same key before it ends. The phone pins the
  key (base64url of SHA-256 over its DER SubjectPublicKeyInfo), so a new certificate never needs a new pairing.
- The phone's keys are 65-byte X9.63 P-256 points; their DER ECDSA/SHA-256 signatures are checked by OpenSSL through
  `cryptography` (strict DER; a high-s signature verifies, which C2's replay memory keyed by r accounts for).
- The signed transcripts T_pair, T_hello and T_proof (v0.3 item 3: bound to the connection's nonce and the call's idem),
  X-1's six-digit code, and the pairing link (3.2).
"""

import base64
import datetime
import hashlib
import os
import re
import secrets
import ssl
import time
from urllib.parse import quote

from . import data_home

KEY_FILE, CERT_FILE = 'tls-key.pem', 'tls-cert.pem'
CERT_DAYS, RENEW_DAYS = 397, 30
_B64U = re.compile(r'[A-Za-z0-9_-]*')
SAS_LABEL = b'pebble-remote/v1/sas\n'
LINK_PREFIX = 'pebble-remote://pair?'


def b64u(data):
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b'=').decode('ascii')


def b64u_decode(text, limit=4096):
    """Strict base64url without padding (one spelling per value), or ValueError."""
    if not isinstance(text, str) or len(text) > limit or len(text) % 4 == 1 or not _B64U.fullmatch(text):
        raise ValueError('not base64url')
    data = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    if b64u(data) != text:
        raise ValueError('not canonical base64url')
    return data


def now_ms():
    return int(time.time() * 1000)


class Identity:
    """The TLS key and certificate in `folder` (0700), made on first use. The key is never replaced here: replacing it
    would change the pin and unpair every phone."""

    def __init__(self, folder):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        self.folder = data_home.private_dir(folder)
        self.key_path, self.cert_path = self.folder / KEY_FILE, self.folder / CERT_FILE
        key = None
        if os.path.lexists(self.key_path):
            with open(self.key_path, 'rb') as handle:
                key = serialization.load_pem_private_key(handle.read(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != 'secp256r1':
                raise ValueError("Pebble Remote's key is not a P-256 key.")
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            data_home.write_atomic(self.key_path, key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.chmod(self.key_path, 0o600)
        spki = key.public_key().public_bytes(serialization.Encoding.DER,
                                             serialization.PublicFormat.SubjectPublicKeyInfo)
        self.pin = b64u(hashlib.sha256(spki).digest())
        if not self._certificate_current(spki):
            data_home.write_atomic(self.cert_path, _certificate(key))
        os.chmod(self.cert_path, 0o600)

    def _certificate_current(self, spki):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        try:
            with open(self.cert_path, 'rb') as handle:
                cert = x509.load_pem_x509_certificate(handle.read())
        except (OSError, ValueError):
            return False
        mine = cert.public_key().public_bytes(serialization.Encoding.DER,
                                              serialization.PublicFormat.SubjectPublicKeyInfo) == spki
        left = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
        return mine and left > datetime.timedelta(days=RENEW_DAYS)

    def tls_context(self):
        """TLS 1.3 only, and no session tickets: every connection shows the certificate, so the phone checks its pin
        every time (security review S-12)."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.options |= ssl.OP_NO_TICKET
        context.num_tickets = 0
        context.load_cert_chain(str(self.cert_path), str(self.key_path))
        return context


def _certificate(key):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Pebble Remote')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


def phone_key(text):
    """A phone's public key from its base64url X9.63 form (65 bytes, a point on P-256), or ValueError."""
    from cryptography.hazmat.primitives.asymmetric import ec
    raw = b64u_decode(text, 128)
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError('not an uncompressed P-256 point')
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)


def signature_r(signature):
    """The r of a DER ECDSA signature (strict DER), or ValueError."""
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    if not isinstance(signature, bytes) or not 8 <= len(signature) <= 72:
        raise ValueError('bad signature length')
    return decode_dss_signature(signature)[0]


def verify(key, message, signature):
    """True when `signature` (DER bytes) is the key's ECDSA/SHA-256 signature of `message`. Never raises."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        signature_r(signature)
        key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _lines(*parts):
    """The transcript form: each part one line of text, followed by a newline, in UTF-8."""
    for part in parts:
        if not isinstance(part, str) or '\n' in part:
            raise ValueError('a transcript line must be one line of text')
    return ''.join(part + '\n' for part in parts).encode('utf-8')


def t_pair(bid, nonce, pin, code, device_key, approve_key):
    return _lines('pebble-remote/v1/pair', bid, nonce, pin, code, device_key, approve_key)


def t_hello(bid, device_id, nonce, pin):
    return _lines('pebble-remote/v1/hello', bid, device_id, nonce, pin)


def t_proof(bid, device_id, epoch, nonce, request_id, idem, method, params_json, ts):
    return _lines('pebble-remote/v1/proof', bid, device_id, epoch, nonce, request_id, idem, method,
                  b64u(hashlib.sha256(params_json.encode('utf-8')).digest()), str(ts))


def sas_code(transcript):
    """X-1 (claude-w, 2026-10-30 10:49): the six digits the Mac and the phone both show once `pair` verifies. The first
    4 bytes of SHA-256 over "pebble-remote/v1/sas\\n" and T_pair, a big-endian integer, mod 1,000,000, zero-padded."""
    return '%06d' % (int.from_bytes(hashlib.sha256(SAS_LABEL + transcript).digest()[:4], 'big') % 1000000)


def new_code():
    return b64u(secrets.token_bytes(16))


def format_endpoint(host, port):
    return ('[' + host + ']' if ':' in host else host) + ':' + str(port)


def pairing_link(bid, name, pin, code, endpoints):
    """pebble-remote://pair?v=1&bid=..&name=..&pin=..&code=..&ep=..: every value percent-encoded (RFC 3986)."""
    fields = [('v', '1'), ('bid', bid), ('name', name), ('pin', pin), ('code', code)]
    fields += [('ep', endpoint) for endpoint in endpoints]
    return LINK_PREFIX + '&'.join(key + '=' + quote(value, safe='') for key, value in fields)
