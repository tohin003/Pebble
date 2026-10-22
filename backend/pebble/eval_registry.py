"""Phase 18 evidence registry: a hash-chained record of every evaluation started.

The registry lives in the private evidence ledger directory as ``registry.jsonl``
(mode 0600, owned by this user, never a symlink). Each line is one entry:

- ``plan``: a campaign plan, its thresholds digest and its planned cell count,
  registered before any live call;
- ``begin``: an evaluation's ID, its plan and the exact planned cells, written before
  the first owned App of that evaluation starts;
- ``end``: the SHA-256 of the signed report the evaluation produced.

Each entry carries ``prev`` (the previous entry's hash; 64 zeros for the first)
and ``hash`` (SHA-256 of its canonical body). Reordering, insertion, editing or
removing an inner entry breaks the chain. A chain alone cannot show that its
newest entries were cut (Q5 F1), so every append also rewrites a head anchor,
``registry.head``: the entry count and last hash under an HMAC with a private
registry key. ``verify_chain`` and every append refuse a registry shorter than
its anchor, so a begun evaluation cannot be cut and re-run unnoticed by anyone
without the ledger's key. Block reports record their own ``begin`` position, and
the parent can witness ``head()`` outside the ledger at each sitting.

Residual (R1): anyone with write access to the ledger directory holds the key and
can rebuild a consistent chain and anchor; only a witnessed head recorded
elsewhere, or Phase 19's external attestation, shows that.
"""

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone

GENESIS = '0' * 64
NAME = 'registry.jsonl'
ANCHOR = 'registry.head'
KEY = 'registry.key'
KINDS = ('plan', 'begin', 'end')
ARMS_18 = ('pebble', 'baseline')
MAX_CELLS = 4096
MAX_PLAN_CELLS = 1_000_000
MAX_BYTES = 16_000_000
HEX32 = re.compile(r'[0-9a-f]{32}')
HEX64 = re.compile(r'[0-9a-f]{64}')
TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


class RegistryRefused(ValueError):
    """A registry write or read was refused; ``reasons`` are stable codes."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__(', '.join(self.reasons))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def entry_hash(entry):
    return _sha(_canonical({k: v for k, v in entry.items() if k != 'hash'}))


def valid_cells(cells):
    """Cells are [task_id, arm, block] triples: unique, bounded and well formed."""
    if not isinstance(cells, list) or not 1 <= len(cells) <= MAX_CELLS:
        return False
    seen = set()
    for cell in cells:
        if (not isinstance(cell, list) or len(cell) != 3 or not isinstance(cell[0], str)
                or not TASK_ID.fullmatch(cell[0]) or cell[1] not in ARMS_18
                or type(cell[2]) is not int or not 1 <= cell[2] <= 64):
            return False
        if tuple(cell) in seen:
            return False
        seen.add(tuple(cell))
    return True


def _private_fd(fd):
    info = os.fstat(fd)
    return (stat.S_ISREG(info.st_mode) and not stat.S_IMODE(info.st_mode) & 0o077
            and not (hasattr(os, 'getuid') and info.st_uid != os.getuid()))


class EvidenceRegistry:
    """Append-only, hash-chained registry with a keyed head anchor, inside an ``EvidenceLedger`` directory."""

    def __init__(self, ledger):
        root = getattr(ledger, 'root', None)
        if root is None:
            raise RegistryRefused(['ledger_invalid'])
        self.root = root
        self.path = root / NAME
        self.anchor_path = root / ANCHOR

    # -- file access ---------------------------------------------------------------------

    def _open(self, write):
        flags = (os.O_RDWR | os.O_CREAT | os.O_APPEND if write else os.O_RDONLY) | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileNotFoundError:
            return None
        except OSError:
            raise RegistryRefused(['registry_unreadable']) from None
        if not _private_fd(fd) or os.fstat(fd).st_size > MAX_BYTES:
            os.close(fd)
            raise RegistryRefused(['registry_not_private'])
        return fd

    @staticmethod
    def _read_fd(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        data = b''.join(chunks)
        if len(data) > MAX_BYTES:
            raise RegistryRefused(['registry_too_large'])
        entries = []
        for line in data.split(b'\n'):
            if not line:
                continue
            try:
                entry = json.loads(line.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                raise RegistryRefused(['registry_malformed']) from None
            if not isinstance(entry, dict):
                raise RegistryRefused(['registry_malformed'])
            entries.append(entry)
        if data and not data.endswith(b'\n'):
            raise RegistryRefused(['registry_truncated_line'])
        return entries

    def _key(self):
        """The private registry key (created once, like the ledger's route salt)."""
        path, flags = self.root / KEY, getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600)
        except FileExistsError:
            fd = None
        except OSError:
            raise RegistryRefused(['registry_key_unavailable']) from None
        if fd is not None:
            value = secrets.token_hex(32)
            try:
                os.write(fd, value.encode())
            finally:
                os.close(fd)
            return bytes.fromhex(value)
        try:
            fd = os.open(path, os.O_RDONLY | flags)
        except OSError:
            raise RegistryRefused(['registry_key_unavailable']) from None
        try:
            if not _private_fd(fd):
                raise RegistryRefused(['registry_not_private'])
            raw = os.read(fd, 129).decode('ascii', 'replace').strip()
        finally:
            os.close(fd)
        if not re.fullmatch(r'[0-9a-f]{64}', raw):
            raise RegistryRefused(['registry_key_corrupt'])
        return bytes.fromhex(raw)

    def _mac(self, count, head):
        return hmac.new(self._key(), ('%d:%s' % (count, head)).encode(), hashlib.sha256).hexdigest()

    def _read_anchor(self):
        try:
            fd = os.open(self.anchor_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return None
        except OSError:
            raise RegistryRefused(['registry_head_unreadable']) from None
        try:
            if not _private_fd(fd):
                raise RegistryRefused(['registry_not_private'])
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        try:
            anchor = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise RegistryRefused(['registry_head_mismatch']) from None
        if (not isinstance(anchor, dict) or type(anchor.get('entries')) is not int or anchor['entries'] < 1
                or not HEX64.fullmatch(str(anchor.get('head'))) or not isinstance(anchor.get('mac'), str)):
            raise RegistryRefused(['registry_head_mismatch'])
        return anchor

    def _write_anchor(self, count, head):
        body = _canonical({'entries': count, 'head': head, 'mac': self._mac(count, head)}).encode()
        temp = self.root / ('%s.%s.tmp' % (ANCHOR, secrets.token_hex(8)))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, self.anchor_path)

    def _check_anchor(self, entries):
        """The registry may not be shorter than its keyed anchor. One extra entry beyond the anchor is
        the crash window between the append and the anchor rewrite; it is accepted and re-anchored."""
        anchor = self._read_anchor()
        if anchor is None:
            if entries:
                raise RegistryRefused(['registry_head_missing'])
            return
        if not hmac.compare_digest(anchor['mac'], self._mac(anchor['entries'], anchor['head'])):
            raise RegistryRefused(['registry_head_mismatch'])
        count = anchor['entries']
        if not count <= len(entries) <= count + 1 or entries[count - 1]['hash'] != anchor['head']:
            raise RegistryRefused(['registry_head_mismatch'])

    def _append(self, body):
        fd = self._open(write=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            entries = self._read_fd(fd)
            self._check(entries)
            self._check_anchor(entries)
            self._admit(entries, body)
            entry = {**body, 'seq': len(entries), 'prev': entries[-1]['hash'] if entries else GENESIS, 'at': _now()}
            entry['hash'] = entry_hash(entry)
            line = (_canonical(entry) + '\n').encode('utf-8')
            if os.write(fd, line) != len(line):
                raise RegistryRefused(['registry_write_incomplete'])
            os.fsync(fd)
            self._write_anchor(len(entries) + 1, entry['hash'])
            return entry
        finally:
            os.close(fd)

    # -- validation ----------------------------------------------------------------------

    @staticmethod
    def _check(entries):
        prev, begun, ended, plans = GENESIS, {}, set(), set()
        for index, entry in enumerate(entries):
            if entry.get('seq') != index or entry.get('prev') != prev or entry.get('kind') not in KINDS:
                raise RegistryRefused(['registry_chain_broken'])
            if not isinstance(entry.get('hash'), str) or entry_hash(entry) != entry['hash']:
                raise RegistryRefused(['registry_chain_broken'])
            kind = entry['kind']
            if kind == 'plan':
                cells = entry.get('planned_cells')
                if (not HEX64.fullmatch(str(entry.get('plan_sha256'))) or not HEX64.fullmatch(str(entry.get('thresholds_sha256')))
                        or entry['plan_sha256'] in plans
                        or cells is not None and (type(cells) is not int or not 1 <= cells <= MAX_PLAN_CELLS)):
                    raise RegistryRefused(['registry_chain_broken'])
                plans.add(entry['plan_sha256'])
            elif kind == 'begin':
                ident = entry.get('evaluation_id')
                if (not HEX32.fullmatch(str(ident)) or ident in begun or not HEX64.fullmatch(str(entry.get('plan_sha256')))
                        or not valid_cells(entry.get('cells'))):
                    raise RegistryRefused(['registry_chain_broken'])
                begun[ident] = entry
            else:
                ident = entry.get('evaluation_id')
                if ident not in begun or ident in ended or not HEX64.fullmatch(str(entry.get('report_sha256'))):
                    raise RegistryRefused(['registry_chain_broken'])
                ended.add(ident)
            prev = entry['hash']

    @staticmethod
    def _admit(entries, body):
        kind = body['kind']
        if kind == 'plan':
            if any(e['kind'] == 'plan' and e['plan_sha256'] == body['plan_sha256'] for e in entries):
                raise RegistryRefused(['plan_already_registered'])
        elif kind == 'begin':
            if not any(e['kind'] == 'plan' and e['plan_sha256'] == body['plan_sha256'] for e in entries):
                raise RegistryRefused(['plan_not_registered'])
            if any(e.get('evaluation_id') == body['evaluation_id'] for e in entries):
                raise RegistryRefused(['evaluation_already_begun'])
        else:
            if not any(e['kind'] == 'begin' and e['evaluation_id'] == body['evaluation_id'] for e in entries):
                raise RegistryRefused(['evaluation_not_begun'])
            if any(e['kind'] == 'end' and e['evaluation_id'] == body['evaluation_id'] for e in entries):
                raise RegistryRefused(['evaluation_already_ended'])

    # -- public API ----------------------------------------------------------------------

    def register_plan(self, plan_sha256, thresholds_sha256, planned_cells=None):
        """Pre-register a campaign plan, its thresholds digest and (for live campaigns) its planned cell count."""
        if not isinstance(plan_sha256, str) or not HEX64.fullmatch(plan_sha256):
            raise RegistryRefused(['plan_sha256_invalid'])
        if not isinstance(thresholds_sha256, str) or not HEX64.fullmatch(thresholds_sha256):
            raise RegistryRefused(['thresholds_sha256_invalid'])
        if planned_cells is not None and (type(planned_cells) is not int or not 1 <= planned_cells <= MAX_PLAN_CELLS):
            raise RegistryRefused(['planned_cells_invalid'])
        body = {'kind': 'plan', 'plan_sha256': plan_sha256, 'thresholds_sha256': thresholds_sha256}
        if planned_cells is not None:
            body['planned_cells'] = planned_cells
        return self._append(body)

    def begin(self, evaluation_id, plan_sha256, cells):
        """Record a start before the evaluation's first owned App; cells are [task_id, arm, block]."""
        if not isinstance(evaluation_id, str) or not HEX32.fullmatch(evaluation_id):
            raise RegistryRefused(['evaluation_id_invalid'])
        if not isinstance(plan_sha256, str) or not HEX64.fullmatch(plan_sha256):
            raise RegistryRefused(['plan_sha256_invalid'])
        cells = [list(cell) if isinstance(cell, (list, tuple)) else cell for cell in cells] if isinstance(
            cells, (list, tuple)) else cells
        if not valid_cells(cells):
            raise RegistryRefused(['cells_invalid'])
        return self._append({'kind': 'begin', 'evaluation_id': evaluation_id, 'plan_sha256': plan_sha256,
                             'cells': cells})

    def end(self, evaluation_id, report_sha256):
        """Bind the signed report an evaluation produced (SHA-256 of its canonical JSON)."""
        if not isinstance(evaluation_id, str) or not HEX32.fullmatch(evaluation_id):
            raise RegistryRefused(['evaluation_id_invalid'])
        if not isinstance(report_sha256, str) or not HEX64.fullmatch(report_sha256):
            raise RegistryRefused(['report_sha256_invalid'])
        return self._append({'kind': 'end', 'evaluation_id': evaluation_id, 'report_sha256': report_sha256})

    def entries(self):
        """Every entry in order. Structure is checked; call verify_chain() for the chain and its anchor."""
        fd = self._open(write=False)
        if fd is None:
            return []
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            return self._read_fd(fd)
        finally:
            os.close(fd)

    def verify_chain(self):
        """True when every entry links to its predecessor and the registry is not shorter than its anchor."""
        entries = self.entries()
        self._check(entries)
        self._check_anchor(entries)
        return True

    def head(self):
        """{'entries', 'head'} of a verified registry: what the parent witnesses outside the ledger."""
        entries = self.entries()
        self._check(entries)
        self._check_anchor(entries)
        return {'entries': len(entries), 'head': entries[-1]['hash'] if entries else GENESIS}
