"""Build Pebble's public source tree from publish/manifest.json, scrub it and run the publication gates (Phase 24:
V1-022, V1-152). It never commits, pushes, uploads or reaches the network: the commands that make the fresh history
and push it are printed for the parent, to run after the person's explicit go.

  publish_tree.py --out NEW_FOLDER --report NEW_FILE.json [--source CHECKOUT] [--manifest publish/manifest.json]
                  [--markers-file FILE] [--inventory-report FILE] [--gitleaks PATH] [--shellcheck PATH]
                  [--python PATH] [--node PATH] [--node-modules DIR] [--scratch NEW_FOLDER]
                  [--test-file REL ...] [--skip-tests]

1. Selection. The checkout is walked without following links (the manifest's prune folders are never entered). A file
   is selected only when it matches an include pattern and no exclude pattern. A selected link or special file, or a
   selected file that matches a never pattern, refuses the tree before anything is written.
2. The tree. --out must not exist. Every selected file is copied byte for byte (mode 0755 when the source is
   executable, else 0644), and the manifest's scrubs (a pattern, its literal replacement and the exact number of
   matches) are applied to the copies. The checkout is never written.
3. Gates, each passed, failed or not_run with its evidence:
   - never_paths: no published path matches a never pattern;
   - gitignore: the published .gitignore ignores no published file (the gitignore subset that file uses);
   - machine_paths: no /Users/<name> outside the manifest's synthetic names, and none of this Mac's own paths (the
     home folder and the checkout);
   - emails: no address outside the reserved example domains and the manifest's reviewed list;
   - account_folders: no Claude account folder (a dot-claude-dash folder), whatever its name;
   - private_markers: none of the lines of --markers-file (default <checkout>/.runtime/publish-private-markers.txt,
     which is never published), compared case-insensitively; a hit names the marker only by its line number. The
     file holds what no public rule may name: e-mail addresses, the private folder and repository names, and the
     development accounts' labels;
   - gitleaks: `gitleaks dir` with the tree's own .gitleaks.toml reports nothing;
   - shellcheck: `bash -n` and shellcheck pass on every published *.sh;
   - inventory: --inventory-report (scripts/release_inventory.py's report for the release candidate) says complete
     and was made from exactly the published notices and lock files;
   - clean_clone_tests: in a fresh copy of the tree, plus the manifest's provisioned build inputs (copied from the
     checkout after their SHA-256 is checked) and node_modules (--node-modules, what npm ci makes from the same
     package-lock.json), every published backend test file passes on its own (one pytest process per file, its own
     --basetemp, a new HOME) and every published *.test.mjs passes under node --test. Skipped when an earlier gate
     failed.
4. Report. --report, a new file outside the tree, holds the selection counts, every published file with its SHA-256,
   the tree digest, the scrubs, every gate, and (for review, not a gate) the Markdown links that point at files left
   unpublished. Nothing in it is published.

Exit codes: 0 publishable (every gate passed; the parent's commands are printed), 3 refused (a hit, a failed gate or
a refused input), 5 incomplete (a gate was not run, or only some test files ran), 2 usage, 1 crash.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'publish/manifest.json'
FORMAT = 'pebble.publish-manifest/1'
REPORT_FORMAT = 'pebble.publish-report/1'
INVENTORY_FORMAT = 'pebble.release-inventory/1'
MARKERS = '.runtime/publish-private-markers.txt'
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
MAX_HITS = 200
TEST_TIMEOUT = 1800
GATES = ('never_paths', 'gitignore', 'machine_paths', 'emails', 'account_folders', 'private_markers', 'gitleaks',
         'shellcheck', 'inventory', 'clean_clone_tests')
STATIC = GATES[:-1]
USER_PATH = re.compile(r'/Users/([A-Za-z0-9._-]+)')
EMAIL = re.compile(r'(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})')
# A Claude account folder (~/.claude-<name>, with its session paths; D11), whatever its name. A dotted model ID such as
# "anthropic.claude-3" is not a folder: the dot follows a letter there. The development accounts' own labels are
# private markers (the markers file), so no public rule names them.
ACCOUNT = re.compile(r'(?<![A-Za-z0-9_])\.claude-[A-Za-z0-9]')
SHA256 = re.compile(r'[0-9a-f]{64}')
PUBLISHED_LOCKS = {'package_lock': 'package-lock.json', 'cargo_lock': 'src-tauri/Cargo.lock',
                   'requirements': 'backend/requirements.txt'}
RESOURCE_NOTICES = ('NOTICE.md', 'AGENTGUARD-LICENSE', 'LICENSE')


class Refused(Exception):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def run(argv, cwd=None, env=None, timeout=None):
    """The real runner: no shell, no stdin, the given environment; (exit code, the last 4,000 characters)."""
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, 'timed out after %s s' % timeout
    except OSError as error:
        return 127, str(error)
    return done.returncode, (done.stdout + done.stderr)[-4000:]


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def glob_regex(pattern):
    """A manifest pattern as a full-path regex: ** spans folders, * and ? stay inside one name."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
        elif pattern.startswith('**', i):
            out.append('.*')
            i += 2
        elif pattern[i] == '*':
            out.append('[^/]*')
            i += 1
        elif pattern[i] == '?':
            out.append('[^/]')
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile(''.join(out) + r'\Z', re.S)


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _globs(value, *keys):
    return isinstance(value, list) and all(isinstance(item, dict) and all(isinstance(item.get(k), str) and item.get(k)
                                                                           for k in ('glob', 'reason') + keys)
                                           for item in value)


def load_manifest(path):
    try:
        manifest = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as error:
        raise Refused('manifest_unreadable', str(error)) from None
    problems = []
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT:
        raise Refused('manifest_invalid', 'format')
    for key in ('prune', 'include'):
        if not _strings(manifest.get(key)):
            problems.append(key)
    for key in ('exclude', 'never'):
        if not _globs(manifest.get(key)):
            problems.append(key)
    scrubs = manifest.get('scrub')
    if not isinstance(scrubs, list) or not all(
            isinstance(s, dict) and all(isinstance(s.get(k), str) and s.get(k) for k in ('path', 'pattern', 'reason'))
            and isinstance(s.get('replace'), str) and type(s.get('count')) is int and s['count'] >= 1
            for s in scrubs):
        problems.append('scrub')
    else:
        for rule in scrubs:
            try:
                rule['regex'] = re.compile(rule['pattern'].encode())
            except re.error:
                problems.append('scrub')
    scan = manifest.get('scan')
    if not isinstance(scan, dict) or not _strings(scan.get('synthetic_user_names')) \
            or not _strings(scan.get('reserved_email_domains')) \
            or not _globs(scan.get('allowed_email_paths')) \
            or not isinstance(scan.get('allowed_emails'), list) or not all(
                isinstance(a, dict) and isinstance(a.get('address'), str) and _strings(a.get('paths'))
                and isinstance(a.get('reason'), str) and a['reason'] for a in scan['allowed_emails']):
        problems.append('scan')
    clean = manifest.get('clean_clone')
    if not isinstance(clean, dict) or not isinstance(clean.get('provision'), list) or not all(
            isinstance(p, dict) and isinstance(p.get('path'), str) and p['path']
            and isinstance(p.get('sha256'), str) and SHA256.fullmatch(p['sha256']) for p in clean['provision']) \
            or not isinstance(clean.get('deselect'), list) or not all(
                isinstance(d, dict) and isinstance(d.get('test'), str) and isinstance(d.get('reason'), str)
                and d['reason'] for d in clean['deselect']):
        problems.append('clean_clone')
    if problems:
        raise Refused('manifest_invalid', problems)
    return manifest


def _first(patterns, rel):
    return next((glob for regex, glob in patterns if regex.match(rel)), None)


def select(source, manifest):
    """{selected, excluded, not_included, pruned, problems}: problems are links, special files and never paths."""
    source = Path(source)
    include = [(glob_regex(p), p) for p in manifest['include']]
    exclude = [(glob_regex(e['glob']), e['glob']) for e in manifest['exclude']]
    never = [(glob_regex(e['glob']), e['glob']) for e in manifest['never']]
    prune = set(manifest['prune'])
    selected, excluded, not_included, pruned, problems = [], {}, {}, [], []

    def consider(rel, info):
        if _first(include, rel) is None:
            top = rel.split('/', 1)[0] if '/' in rel else '(root files)'
            not_included[top] = not_included.get(top, 0) + 1
            return
        glob = _first(exclude, rel)
        if glob is not None:
            excluded.setdefault(glob, []).append(rel)
        elif stat.S_ISLNK(info.st_mode):
            problems.append({'code': 'link_selected', 'path': rel})
        elif not stat.S_ISREG(info.st_mode):
            problems.append({'code': 'special_file_selected', 'path': rel})
        else:
            selected.append(rel)

    for folder, dirs, files in os.walk(source, followlinks=False):
        base = Path(folder).relative_to(source).as_posix()
        base = '' if base == '.' else base + '/'
        keep = []
        for name in sorted(dirs):
            rel, full = base + name, os.path.join(folder, name)
            info = os.lstat(full)
            if rel in prune:
                pruned.append(rel)
            elif stat.S_ISLNK(info.st_mode):
                consider(rel, info)  # a link to a folder: never followed, refused when selected
            else:
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            consider(base + name, os.lstat(os.path.join(folder, name)))
    selected.sort()
    for rel in selected:
        glob = _first(never, rel)
        if glob is not None:
            problems.append({'code': 'never_published_present', 'path': rel, 'glob': glob})
    return {'selected': selected, 'excluded': {glob: sorted(paths) for glob, paths in sorted(excluded.items())},
            'not_included': dict(sorted(not_included.items())), 'pruned': sorted(pruned), 'problems': problems}


def read_regular(path):
    """(bytes, executable) of a regular file, opened without following a link."""
    fd = os.open(path, os.O_RDONLY | NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused('special_file_selected', str(path))
        return stream.read(), bool(info.st_mode & 0o111)


def scrubbed(manifest, selected):
    """{path: [rule, ...]}, refused when a rule names a file that is not published."""
    rules = {}
    for rule in manifest['scrub']:
        if rule['path'] not in selected:
            raise Refused('scrub_path_not_published', rule['path'])
        rules.setdefault(rule['path'], []).append(rule)
    return rules


def apply_scrubs(rel, data, rules):
    records = []
    for rule in rules.get(rel, ()):
        replace = rule['replace'].encode()
        data, found = rule['regex'].subn(lambda match: replace, data)  # the replacement is literal text
        if found != rule['count']:
            raise Refused('scrub_count_differs', {'path': rel, 'expected': rule['count'], 'found': found})
        records.append({'path': rel, 'count': found, 'reason': rule['reason']})
    return data, records


def write_new(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    os.chmod(path, mode)


def inside(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip('/') + '/')


def build(source, out, manifest, selection):
    """Copy the selection into the new folder out, with the scrubs; [{path, sha256, bytes, mode}], scrub records."""
    selected = set(selection['selected'])
    rules = scrubbed(manifest, selected)
    out.mkdir(mode=0o755)
    files, records = [], []
    for rel in selection['selected']:
        data, executable = read_regular(Path(source) / rel)
        source_sha = sha_bytes(data)
        data, done = apply_scrubs(rel, data, rules)
        for record in done:
            records.append(dict(record, source_sha256=source_sha, published_sha256=sha_bytes(data)))
        mode = 0o755 if executable else 0o644
        write_new(out / rel, data, mode)
        files.append({'path': rel, 'sha256': sha_bytes(data), 'bytes': len(data), 'mode': '%o' % mode})
    return files, records


def tree_digest(files):
    lines = ''.join('%s\t%s\t%s\n' % (f['path'], f['mode'], f['sha256']) for f in sorted(files, key=lambda f: f['path']))
    return sha_bytes(lines.encode())


# ---- Content gates ------------------------------------------------------------------------------------------------


def _line(text, position):
    return text.count('\n', 0, position) + 1


class Hits:
    def __init__(self):
        self.items, self.total = [], 0

    def add(self, **item):
        self.total += 1
        if len(self.items) < MAX_HITS:
            self.items.append(item)

    def gate(self, checked):
        return {'status': 'failed' if self.total else 'passed', 'files_checked': checked, 'hits': self.total,
                'items': self.items}


def own_paths(source):
    """This Mac's own paths that must never be published: the home folder and the checkout (as given and resolved)."""
    found = set()
    home = os.path.expanduser('~')
    for path in (home, os.path.realpath(home), str(source), os.path.realpath(source)):
        if path.startswith('/') and path.count('/') >= 2:
            found.add(path.rstrip('/'))
    return sorted(found, key=len, reverse=True)


def read_markers(path):
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return [(number, line.strip().casefold()) for number, line in enumerate(lines, 1)
            if line.strip() and not line.strip().startswith('#')]


def scan(read, paths, manifest, source, markers):
    """machine_paths, emails, account_folders and private_markers over every published file's bytes (read(path))."""
    scan_rules = manifest['scan']
    synthetic = set(scan_rules['synthetic_user_names'])
    reserved = [d.lower() for d in scan_rules['reserved_email_domains']]
    allowed = [(a['address'].lower(), [glob_regex(p) for p in a['paths']]) for a in scan_rules['allowed_emails']]
    allowed_paths = [glob_regex(entry['glob']) for entry in scan_rules['allowed_email_paths']]
    mine = own_paths(source)
    machine, emails, accounts, private = Hits(), Hits(), Hits(), Hits()
    for rel in paths:
        text = read(rel).decode('latin-1')
        for match in USER_PATH.finditer(text):
            if match.group(1) not in synthetic:
                machine.add(path=rel, line=_line(text, match.start()), rule='user_path', text=match.group(0))
        for path in mine:
            start = text.find(path)
            while start != -1:
                machine.add(path=rel, line=_line(text, start), rule='this_mac', text=path)
                start = text.find(path, start + len(path))
        if not any(regex.match(rel) for regex in allowed_paths):
            for match in EMAIL.finditer(text):
                domain, address = match.group(1).lower(), match.group(0).lower()
                if any(domain == d or domain.endswith('.' + d) for d in reserved):
                    continue
                if any(address == a and any(g.match(rel) for g in globs) for a, globs in allowed):
                    continue
                emails.add(path=rel, line=_line(text, match.start()), address=match.group(0))
        for match in ACCOUNT.finditer(text):
            accounts.add(path=rel, line=_line(text, match.start()), text=match.group(0))
        if markers:
            folded = text.casefold()
            for number, marker in markers:
                start = folded.find(marker)
                while start != -1:
                    private.add(path=rel, line=_line(folded, start), marker_line=number)
                    start = folded.find(marker, start + len(marker))
    checked = len(paths)
    gates = {'machine_paths': machine.gate(checked), 'emails': emails.gate(checked),
             'account_folders': accounts.gate(checked)}
    if markers is None:
        gates['private_markers'] = {'status': 'not_run', 'reason': 'markers_file_missing',
                                    'detail': 'List the private strings (the person\'s e-mail addresses, the private '
                                              'folder and repository names) one per line in the markers file.'}
    elif not markers:
        gates['private_markers'] = {'status': 'not_run', 'reason': 'markers_file_empty'}
    else:
        gates['private_markers'] = dict(private.gate(checked), markers=len(markers))
    return gates


MARKDOWN_LINK = re.compile(r'\]\(([^)\s]+)\)')


def dangling_links(read, paths):
    """Relative Markdown links in published .md files whose target is not published (a phase record left behind, for
    example). Reported for the person to review; not a gate."""
    published, found = set(paths), []
    for rel in paths:
        if not rel.endswith('.md'):
            continue
        for target in MARKDOWN_LINK.findall(read(rel).decode('utf-8', 'replace')):
            path = target.split('#', 1)[0]
            if not path or re.match(r'[a-z][a-z0-9+.-]*:', target):
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(rel), path))
            if resolved not in published and not any(p.startswith(resolved.rstrip('/') + '/') for p in published):
                found.append({'path': rel, 'target': target})
    return found


def gitignore_rules(text):
    """The gitignore subset Pebble's file uses: comments, folder rules (a trailing /), anchored rules (a leading or
    inner /) and unanchored name rules with * ? **. Negation is refused, because this checker does not model it."""
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('!') or line.startswith('\\'):
            raise Refused('gitignore_syntax_unsupported', raw)
        folder = line.endswith('/')
        body = line.rstrip('/')
        anchored = '/' in body
        rules.append((glob_regex(body.lstrip('/')), folder, anchored, raw))
    return rules


def ignored_by(rel, rules):
    parts = rel.split('/')
    for regex, folder, anchored, raw in rules:
        for i in range(1, len(parts) + 1):
            is_folder = i < len(parts)
            if folder and not is_folder:
                continue
            candidate = '/'.join(parts[:i]) if anchored else parts[i - 1]
            if regex.match(candidate):
                return raw
    return None


def gate_gitignore(text, paths):
    """text: the published .gitignore (None when there is none)."""
    if text is None:
        return {'status': 'failed', 'reason': 'gitignore_missing'}
    try:
        rules = gitignore_rules(text)
    except Refused as error:
        return {'status': 'failed', 'reason': error.code, 'detail': error.detail}
    hits = [{'path': rel, 'rule': rule} for rel in paths for rule in [ignored_by(rel, rules)] if rule]
    return {'status': 'failed' if hits else 'passed', 'rules': len(rules), 'ignored_published_files': hits[:MAX_HITS],
            'hits': len(hits)}


def gate_gitleaks(out, gitleaks, report_path, runner, env):
    if not gitleaks:
        return {'status': 'not_run', 'reason': 'tool_missing', 'detail': 'gitleaks was not found.'}
    config = out / '.gitleaks.toml'
    if not config.is_file():
        return {'status': 'failed', 'reason': 'config_missing'}
    argv = [gitleaks, 'dir', str(out), '--config', str(config), '--report-format', 'json', '--report-path',
            str(report_path), '--redact', '--no-banner', '--exit-code', '1']
    code, output = runner(argv, cwd=str(out), env=env, timeout=1800)
    try:
        findings = json.loads(Path(report_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        findings = None
    result = {'argv': argv, 'exit_code': code, 'report': str(report_path)}
    if not isinstance(findings, list):
        return dict(result, status='failed', reason='report_unreadable', output=output[-1000:])
    items = [{'path': f.get('File'), 'line': f.get('StartLine'), 'rule': f.get('RuleID'),
              'fingerprint': f.get('Fingerprint')} for f in findings if isinstance(f, dict)]
    if code != 0 or items:
        return dict(result, status='failed', reason='findings' if items else 'gitleaks_error', findings=len(items),
                    items=items[:MAX_HITS], output=output[-1000:])
    return dict(result, status='passed', findings=0)


def gate_shellcheck(out, files, shellcheck, runner, env):
    scripts = [f['path'] for f in files if f['path'].endswith('.sh')]
    checks = []
    for rel in scripts:
        code, output = runner(['/bin/bash', '-n', str(out / rel)], cwd=str(out), env=env, timeout=60)
        checks.append({'path': rel, 'tool': 'bash -n', 'exit_code': code, 'output': output[-1000:]})
    if scripts and not shellcheck:
        return {'status': 'not_run', 'reason': 'tool_missing', 'detail': 'shellcheck was not found.', 'checks': checks}
    for rel in scripts:
        code, output = runner([shellcheck, '--shell=bash', '--severity=style', rel], cwd=str(out), env=env, timeout=120)
        checks.append({'path': rel, 'tool': 'shellcheck', 'exit_code': code, 'output': output[-2000:]})
    failed = [c for c in checks if c['exit_code'] != 0]
    return {'status': 'failed' if failed else 'passed', 'scripts': scripts, 'checks': checks}


def gate_inventory(out, report_path):
    """The release inventory says complete and was made from exactly the published notices and lock files."""
    if report_path is None:
        return {'status': 'not_run', 'reason': 'no_inventory_report',
                'detail': 'Run scripts/release_inventory.py for the release candidate and pass --inventory-report.'}
    try:
        report = json.loads(Path(report_path).read_bytes())
        inputs = report['inputs']
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'failed', 'reason': 'report_unreadable', 'report': str(report_path)}
    differs = []
    if report.get('format') != INVENTORY_FORMAT:
        differs.append('format')
    for key, rel in PUBLISHED_LOCKS.items():
        published = out / rel
        record = inputs.get(key) if isinstance(inputs.get(key), dict) else {}
        if not published.is_file() or record.get('sha256') != sha_bytes(published.read_bytes()):
            differs.append(rel)
    licenses = out / 'src-tauri/resources/licenses'
    published = {p.relative_to(licenses).as_posix(): sha_bytes(p.read_bytes())
                 for p in sorted(licenses.rglob('*')) if p.is_file()} if licenses.is_dir() else {}
    recorded = (inputs.get('licenses') or {}).get('files') if isinstance(inputs.get('licenses'), dict) else None
    if recorded != published:
        differs.append('src-tauri/resources/licenses')
    notices = {name: sha_bytes((out / 'src-tauri/resources' / name).read_bytes())
               for name in RESOURCE_NOTICES if (out / 'src-tauri/resources' / name).is_file()}
    if inputs.get('resource_notices') != notices:
        differs.append('src-tauri/resources notices')
    result = {'report': str(report_path), 'result': report.get('result'),
              'findings': (report.get('summary') or {}).get('findings')}
    if differs:
        return dict(result, status='failed', reason='inventory_differs_from_tree', differs=differs)
    if report.get('result') != 'complete':
        return dict(result, status='failed', reason='inventory_has_gaps')
    return dict(result, status='passed')


def gate_tests(out, files, source, manifest, *, scratch, python, node, node_modules, only, runner):
    """Every published test file in a fresh copy of the tree; see the module docstring."""
    scratch = Path(scratch)
    scratch.mkdir(mode=0o700)
    clone, home, tmp = scratch / 'clone', scratch / 'home', scratch / 'tmp'
    shutil.copytree(out, clone, symlinks=True)
    for folder in (home, tmp, scratch / 'pytest'):  # pytest does not create a --basetemp's parent
        folder.mkdir(mode=0o700)
    provisioned = []
    for item in manifest['clean_clone']['provision']:
        origin = Path(source) / item['path']
        try:
            data, executable = read_regular(origin)
        except (OSError, Refused):
            return {'status': 'failed', 'reason': 'provision_missing', 'path': item['path'],
                    'detail': item.get('fetch')}
        if sha_bytes(data) != item['sha256']:
            return {'status': 'failed', 'reason': 'provision_differs', 'path': item['path']}
        write_new(clone / item['path'], data, 0o755 if executable else 0o644)
        provisioned.append({'path': item['path'], 'sha256': item['sha256']})
    modules = None
    if node_modules and os.path.isdir(node_modules):
        modules = os.path.realpath(node_modules)
        os.symlink(modules, clone / 'node_modules')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin'), 'HOME': str(home), 'TMPDIR': str(tmp) + '/',
           'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    published = [f['path'] for f in files]
    python_tests = [p for p in published if re.fullmatch(r'backend/tests/test_[^/]+\.py', p)]
    node_tests = [p for p in published if p.endswith('.test.mjs') and p.startswith(('src/', 'browser-extension/'))]
    if only:
        unknown = sorted(set(only) - set(python_tests) - set(node_tests))
        if unknown:
            return {'status': 'failed', 'reason': 'unknown_test_file', 'paths': unknown}
        python_tests = [p for p in python_tests if p in only]
        node_tests = [p for p in node_tests if p in only]
    deselect = [arg for item in manifest['clean_clone']['deselect'] for arg in ('--deselect', item['test'])]
    results = []
    for index, rel in enumerate(python_tests, 1):
        # backend/tests/pytest.ini already turns the cache plugin off.
        argv = [python, '-m', 'pytest', '-c', 'backend/tests/pytest.ini', rel, '-q', '-o',
                'tmp_path_retention_policy=failed', '--basetemp', str(scratch / 'pytest' / ('%04d' % index))] + deselect
        code, output = runner(argv, cwd=str(clone), env=env, timeout=TEST_TIMEOUT)
        last = [line for line in output.splitlines() if line.strip()]
        results.append({'file': rel, 'exit_code': code, 'last_line': last[-1] if last else ''})
    if node_tests:
        if not node or modules is None:
            results.append({'file': 'node --test', 'exit_code': None, 'last_line': 'node or node_modules missing'})
        else:
            # Phase 26: one file at a time, so the avatar's frame fixture measures an uncontended process (its own
            # limits: per-frame cost is real time in this Node process).
            code, output = runner([node, '--test', '--test-concurrency=1'] + node_tests, cwd=str(clone), env=env,
                                  timeout=TEST_TIMEOUT)
            summary = [line for line in output.splitlines() if line.startswith('ℹ ')]
            results.append({'file': 'node --test (%d files)' % len(node_tests), 'exit_code': code,
                            'last_line': ' · '.join(summary[-8:])})
    # pytest exits 5 when a file collects nothing, which is not a failure of the file.
    failed = [r for r in results if r['exit_code'] not in (0, 5)]
    status = 'failed' if failed else ('partial' if only else 'passed')
    return {'status': status, 'clone': str(clone), 'home': str(home), 'python': python, 'node': node,
            'node_modules': modules, 'provisioned': provisioned, 'deselected': manifest['clean_clone']['deselect'],
            'python_files': len(python_tests), 'node_files': len(node_tests), 'failed': failed[:MAX_HITS],
            'results': results}


# ---- Orchestration ------------------------------------------------------------------------------------------------


def parent_commands(out, files, version):
    """Printed, never run: the fresh history, its tag and the push, after the person's explicit go."""
    q = lambda value: "'" + str(value).replace("'", "'\\''") + "'"  # noqa: E731
    return [
        'cd ' + q(out),
        'git init --initial-branch=main',
        'git add --all',
        'test "$(git ls-files | wc -l | tr -d \' \')" = %d  # every published file is tracked' % len(files),
        'git -c user.name=%s -c user.email=%s commit -m %s' % (
            q('Tohin Majumder'), q('<the address GitHub should show; its noreply address keeps a personal one private>'),
            q('Pebble %s' % version)),
        'git tag -a v%s -m %s' % (version, q('Pebble %s' % version)),
        'git remote add origin https://github.com/tohin003/Pebble.git',
        'git push -u origin main',
        'git push origin v%s' % version,
    ]


def verdict(gates):
    statuses = [gate['status'] for gate in gates.values()]
    if 'failed' in statuses:
        return 'refused', 3
    if any(status != 'passed' for status in statuses):
        return 'incomplete', 5
    return 'publishable', 0


def publish(source, out, report_path, *, manifest_path=None, markers_file=None, inventory_report=None, gitleaks=None,
            shellcheck=None, python=None, node=None, node_modules=None, scratch=None, test_files=None,
            skip_tests=False, runner=run):
    source = Path(os.path.abspath(source))
    out, report_path = Path(os.path.abspath(out)), Path(os.path.abspath(report_path))
    manifest_path = Path(manifest_path) if manifest_path else source / MANIFEST
    manifest = load_manifest(manifest_path)
    if os.path.lexists(out):
        raise Refused('out_exists', str(out))
    if not out.parent.is_dir():
        raise Refused('out_parent_missing', str(out.parent))
    if inside(source, out) or (inside(out, source) and not any(inside(out, source / p) for p in manifest['prune'])):
        raise Refused('out_inside_checkout', str(out))
    gitleaks_report = report_path.with_name(report_path.name + '.gitleaks.json')
    for path in (report_path, gitleaks_report):
        if os.path.lexists(path) or inside(path, out):
            raise Refused('report_exists_or_inside_tree', str(path))
    if scratch and (os.path.lexists(scratch) or inside(scratch, out)):
        raise Refused('scratch_exists_or_inside_tree', str(scratch))
    markers_path = Path(markers_file) if markers_file else source / MARKERS
    if markers_file and not markers_path.is_file():
        raise Refused('markers_file_unreadable', str(markers_path))
    if inside(markers_path, out):
        raise Refused('markers_file_inside_tree', str(markers_path))
    report = {'format': REPORT_FORMAT, 'source': str(source), 'out': str(out),
              'manifest': {'path': str(manifest_path), 'sha256': sha_bytes(manifest_path.read_bytes())}}
    selection = select(source, manifest)
    report['selection'] = {'selected': len(selection['selected']), 'pruned': selection['pruned'],
                           'not_included': selection['not_included'],
                           'excluded': {glob: {'count': len(paths), 'paths': paths[:50]}
                                        for glob, paths in selection['excluded'].items()},
                           'problems': selection['problems']}
    gates = {name: {'status': 'not_run', 'reason': 'not_reached'} for name in GATES}
    never = [p for p in selection['problems'] if p['code'] == 'never_published_present']
    gates['never_paths'] = {'status': 'failed' if never else 'passed', 'hits': never}
    if selection['problems']:
        report.update(gates=gates, result='refused', refused='selection', tree_written=False)
        return finish(report, report_path, 3)
    try:
        files, scrubs = build(source, out, manifest, selection)
    except Refused as error:
        report.update(gates=gates, result='refused', refused=error.code, detail=error.detail,
                      tree_written=out.exists())
        return finish(report, report_path, 3)
    report.update(tree_written=True, files=files, file_count=len(files), bytes=sum(f['bytes'] for f in files),
                  tree_sha256=tree_digest(files), scrubs=scrubs)
    paths = [f['path'] for f in files]
    gitignore = out / '.gitignore'
    gates['gitignore'] = gate_gitignore(gitignore.read_text(encoding='utf-8') if gitignore.is_file() else None, paths)
    gates.update(scan(lambda rel: (out / rel).read_bytes(), paths, manifest, source, read_markers(markers_path)))
    links = dangling_links(lambda rel: (out / rel).read_bytes(), paths)
    report['dangling_links'] = {'count': len(links), 'items': links[:MAX_HITS],
                                'note': 'Links to files that are not published; review them, not a gate.'}
    tool_env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin'), 'HOME': os.environ.get('HOME', '/'),
                'LANG': 'C'}
    gates['gitleaks'] = gate_gitleaks(out, gitleaks or shutil.which('gitleaks'), gitleaks_report, runner, tool_env)
    gates['shellcheck'] = gate_shellcheck(out, files, shellcheck or shutil.which('shellcheck'), runner, tool_env)
    gates['inventory'] = gate_inventory(out, inventory_report)
    if skip_tests:
        gates['clean_clone_tests'] = {'status': 'not_run', 'reason': 'skipped'}
    elif any(gates[name]['status'] == 'failed' for name in STATIC):
        gates['clean_clone_tests'] = {'status': 'not_run', 'reason': 'earlier_gate_failed'}
    else:
        default_python = source / '.runtime/agent-env/bin/python'
        gates['clean_clone_tests'] = gate_tests(
            out, files, source, manifest, scratch=scratch or tempfile.mkdtemp(prefix='pebble-publish-') + '/run',
            python=python or (str(default_python) if default_python.is_file() else sys.executable),
            node=node or shutil.which('node'), node_modules=node_modules or str(source / 'node_modules'),
            only=set(test_files) if test_files else None, runner=runner)
    report['gates'] = gates
    result, code = verdict(gates)
    report['result'] = result
    if code == 0:
        try:
            version = json.loads((out / 'package.json').read_text(encoding='utf-8'))['version']
        except (OSError, ValueError, KeyError):
            version = '<version>'
        report['parent_commands'] = parent_commands(out, files, version)
        report['parent_note'] = ('Run only after the person\'s explicit go. This tool never commits, pushes or '
                                 'creates a repository or a release.')
    return finish(report, report_path, code)


def finish(report, report_path, code):
    report['exit_code'] = code
    fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report, code


def summary(report):
    keep = ('result', 'exit_code', 'out', 'file_count', 'tree_sha256', 'refused', 'detail', 'parent_commands',
            'parent_note')
    out = {key: report[key] for key in keep if key in report}
    out['gates'] = {name: {k: v for k, v in gate.items() if k in ('status', 'reason', 'hits', 'findings', 'failed')}
                    for name, gate in report.get('gates', {}).items()}
    return out


def main(argv=None, runner=run):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--out', required=True, help='A new folder for the public tree (outside the checkout, or '
                                                     'under its .runtime).')
    parser.add_argument('--report', required=True, help='A new JSON file, outside the tree.')
    parser.add_argument('--source', default=str(ROOT), help='The checkout (default: this tool\'s).')
    parser.add_argument('--manifest', help='Default: <source>/publish/manifest.json.')
    parser.add_argument('--markers-file', help='Default: <source>/' + MARKERS)
    parser.add_argument('--inventory-report', help='scripts/release_inventory.py\'s report for the release candidate.')
    parser.add_argument('--gitleaks', help='Default: gitleaks on PATH.')
    parser.add_argument('--shellcheck', help='Default: shellcheck on PATH.')
    parser.add_argument('--python', help='The interpreter for the clean-clone tests (default: '
                                         '<source>/.runtime/agent-env/bin/python).')
    parser.add_argument('--node', help='Default: node on PATH.')
    parser.add_argument('--node-modules', help='Default: <source>/node_modules.')
    parser.add_argument('--scratch', help='A new folder for the clean clone and the test scratch.')
    parser.add_argument('--test-file', action='append', help='Run only this published test file (repeatable); the '
                                                             'tests gate is then partial.')
    parser.add_argument('--skip-tests', action='store_true', help='Do not run the clean-clone tests (incomplete).')
    args = parser.parse_args(argv)
    try:
        report, code = publish(args.source, args.out, args.report, manifest_path=args.manifest,
                               markers_file=args.markers_file, inventory_report=args.inventory_report,
                               gitleaks=args.gitleaks, shellcheck=args.shellcheck, python=args.python, node=args.node,
                               node_modules=args.node_modules, scratch=args.scratch, test_files=args.test_file,
                               skip_tests=args.skip_tests, runner=runner)
    except Refused as error:
        print(json.dumps({'result': 'refused', 'refused': error.code, 'detail': error.detail}, indent=2))
        return 3
    print(json.dumps(summary(report), indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
