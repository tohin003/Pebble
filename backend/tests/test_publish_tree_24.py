"""scripts/publish_tree.py, publish/manifest.json, .gitleaks.toml and .gitignore (Phase 24: V1-022, V1-152, V1-134).

The real checkout is selected and scanned in memory (nothing is copied or written): no private path, user path,
e-mail address or account folder may be published. The gates themselves run on small trees in tmp_path with an
injected runner; one end-to-end run uses fake gitleaks and shellcheck executables, the real runner, a real pytest and a
real node --test in the clean clone. Nothing is committed, pushed or fetched. Planted markers are put together at run
time, so this file itself scans clean.
"""

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('publish_tree_under_test', REPO / 'scripts' / 'publish_tree.py')
pt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pt)
MANIFEST = pt.load_manifest(REPO / 'publish' / 'manifest.json')
# Synthetic stand-ins only: no real name, folder, account or address is written here, even in pieces.
USERS = '/' + 'Users' + '/'
ACCOUNT_DIR = '.' + 'claude-' + 'alpha'
PERSON = 'jane.doe' + '@' + 'mailbox.co'
FOLDER_MARKER = 'Lantern ' + 'Garden (draft 7)'
NEW_ROOT_FILES = ('LICENSE', 'NOTICE.md', 'README.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md',
                  'CODE_OF_CONDUCT.md', 'BUILDING.md', '.gitignore', '.gitleaks.toml', 'install.sh',
                  'publish/manifest.json', 'docs/USER_GUIDE.md', 'docs/RELEASE.md', 'docs/MODEL_SELECTION.md')


def put(path, data=b'', mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(mode)
    return path


def sha(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


# ---- The real checkout ------------------------------------------------------------------------------------------


@pytest.fixture(scope='module')
def real():
    selection = pt.select(REPO, MANIFEST)
    rules = pt.scrubbed(MANIFEST, set(selection['selected']))
    return selection, rules


def test_the_real_tree_selects_no_private_path(real):
    selection, _ = real
    assert selection['problems'] == []
    selected = set(selection['selected'])
    for private in ('.runtime', 'reports', 'node_modules'):  # absent in a clean clone; never entered when present
        assert (private in selection['pruned']) == os.path.lexists(REPO / private), private
    private = re.compile(r'(^|/)(\.runtime|reports|release|models|node_modules|dist|__pycache__|\.git)/|'
                         r'^src-tauri/(target|resources/(backend|llama|helpers|tokenizer))/|'
                         r'\.(gguf|safetensors|dmg|log|pyc|sqlite|db|pem|p12|keychain-db)$|(^|/)\.env$')
    assert sorted(rel for rel in selected if private.search(rel)) == []
    history = re.compile(r'^docs/(CLAUDE_HANDOFF_|HARNESS_PHASE|PEBBLE_V1_INSPECTION|PEBBLE_FRONTIER_RUBRIC|'
                         r'RELEASE_V0|HARNESS_IMPLEMENTATION)|^scripts/finalize_harness_phase\d+\.py$')
    assert sorted(rel for rel in selected if history.search(rel)) == []  # D11
    # Every test file published can run: the one that loads an unpublished finalizer stays out with it.
    assert 'backend/tests/test_phase13_evidence.py' not in selected
    loads_finalizer = re.compile(r'finalize_harness_phase\d+\.py')
    for rel in selected:
        if rel.startswith('backend/tests/') and rel.endswith('.py'):
            assert not loads_finalizer.search((REPO / rel).read_text(encoding='utf-8', errors='replace')), rel
    # What a clean clone needs to build and test is there.
    for rel in ('build/pebble-backend.spec', 'rust-toolchain.toml', 'backend/requirements.lock', 'package-lock.json',
                'src-tauri/Cargo.lock', 'src-tauri/resources/LICENSE', 'src-tauri/resources/NOTICE.md',
                'src-tauri/resources/model.json', 'src-tauri/gen/schemas/desktop-schema.json',
                'src-tauri/helpers/pebble-vault/pebble_vault.c', 'backend/tests/pytest.ini', 'scripts/publish_tree.py',
                'scripts/package_release.py', 'backend/tests/fixtures/phase19/build_epoch8_fixture.py',
                'browser-extension/manifest.json', 'docs/DATA_FOLDERS.md'):
        assert rel in selected, rel


def test_the_new_root_documents_are_published(real):
    selection, _ = real
    assert sorted(set(NEW_ROOT_FILES) - set(selection['selected'])) == []


def test_the_real_tree_has_no_user_path_e_mail_or_account_folder_after_scrubbing(real):
    """0 /Users/<name> (besides the reviewed synthetic test names), 0 addresses (besides reserved example domains and
    the reviewed list), 0 account folders or labels, and none of this Mac's own paths."""
    selection, rules = real
    scrubbed, already = {}, set()

    def read(rel):
        data = (REPO / rel).read_bytes()
        if rel in rules and all(not rule['regex'].search(data) and rule['replace'].encode() in data
                                for rule in rules[rel]):
            already.add(rel)  # a published copy (a clean clone): its scrubs are already applied
            return data
        data, records = pt.apply_scrubs(rel, data, rules)
        scrubbed.update({record['path']: record['count'] for record in records})
        return data
    gates = pt.scan(read, selection['selected'], MANIFEST, REPO, None)
    for name in ('machine_paths', 'emails', 'account_folders'):
        assert gates[name]['hits'] == 0, (name, gates[name]['items'][:20])
        assert gates[name]['files_checked'] == len(selection['selected'])
    assert gates['private_markers']['status'] == 'not_run'  # the parent's markers file is never in the checkout
    assert scrubbed == {'scripts/audit_dev_workers.py': 1} or (scrubbed == {} and already == {'scripts/audit_dev_workers.py'})
    # The scrub keeps the auditor at least as strict: its private-path pattern now matches every account folder.
    published = read('scripts/audit_dev_workers.py').decode()
    pattern = re.search(r"PRIVATE = re\.compile\(r'([^']+)'", published).group(1)
    assert re.search(pattern, '/x/' + ACCOUNT_DIR + '/projects/a', re.I)
    assert re.search(pattern, '/x/.claude-' + 'beta/projects/a', re.I)


OWNED_DOCS = ('README.md', 'NOTICE.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md', 'CODE_OF_CONDUCT.md',
              'BUILDING.md', 'docs/USER_GUIDE.md', 'docs/RELEASE.md', 'docs/MODEL_SELECTION.md')


def slug(heading):
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r'[^\w\- ]', '', heading.strip().lower()).replace(' ', '-')


def test_the_documents_link_only_to_published_files_and_real_headings(real):
    selection, _ = real
    selected = set(selection['selected'])
    checked = 0
    for doc in OWNED_DOCS:
        text = (REPO / doc).read_text(encoding='utf-8')
        for target in re.findall(r'\]\(([^)\s]+)\)', text):
            if re.match(r'[a-z]+:', target):
                continue
            path, _, anchor = target.partition('#')
            resolved = os.path.normpath(os.path.join(os.path.dirname(doc), path)) if path else doc
            assert resolved in selected, (doc, target)  # never a link into an unpublished record
            if anchor:
                headings = re.findall(r'^#{1,6} (.+)$', (REPO / resolved).read_text(encoding='utf-8'), re.M)
                assert anchor in {slug(h) for h in headings}, (doc, target)
            checked += 1
    assert checked > 40


def test_the_licence_is_apache_2_and_the_notices_agree():
    licence = (REPO / 'LICENSE').read_bytes()
    assert licence == (REPO / 'src-tauri/resources/LICENSE').read_bytes() == (REPO / 'backend/vendor/AGENTGUARD-LICENSE').read_bytes()
    assert len(licence) == 11357 and licence.startswith(b'                                 Apache License\n')
    notice, app_notice = (REPO / 'NOTICE.md').read_text(), (REPO / 'src-tauri/resources/NOTICE.md').read_text()
    for words in ('Copyright 2026 Tohin Majumder', 'Apache License, Version 2.0', 'THIRD-PARTY-NOTICES.txt', 'AgentGuard',
                  '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d', 'Qwen3.5-4B', 'bloub-LICENSE',
                  'llama.cpp-LICENSE', 'not a security sandbox'):
        assert words in notice and words in app_notice, words
    assert 'licenses/HEIF' not in notice and 'pillow-heif' not in notice.lower()
    # D10: the only model pages linked are public model cards; the personal fine-tune is not published.
    models = re.findall(r'huggingface\.co/([\w.-]+/[\w.-]+)', (REPO / 'docs/MODEL_SELECTION.md').read_text() + notice)
    assert set(models) == {'microsoft/FrogNano-4B-2609', 'Qwen/Qwen3.5-4B', 'janhq/Jan-code-4b',
                           'ibm-granite/granite-4.0-h-tiny', 'bartowski/FrogNano-4B-2609-GGUF'}
    assert 'personal' not in (REPO / 'docs/MODEL_SELECTION.md').read_text().lower()


def test_the_user_guide_quotes_the_apps_own_words():
    """V1-103 and V1-113 (D9): the permission modes, the search destination, the tool switches, the connector scope and
    the shortcuts, exactly as Pebble shows them."""
    guide = ' '.join((REPO / 'docs/USER_GUIDE.md').read_text(encoding='utf-8').split())
    modes = (REPO / 'src/permissionModes.ts').read_text(encoding='utf-8')
    found = re.findall(r'mode\(\s*"(\w+)",\s*"([^"]+)",\s*"([^"]+)",\s*"([^"]+)",?\s*\)', modes)
    assert [mode[0] for mode in found] == ['ask', 'auto', 'full']
    for _, name, runs, asks in found:
        assert '| **%s**' % name in guide and '| %s | %s |' % (runs, asks) in guide, name
    assert re.search(r'EVERY_MODE =\s*"([^"]+)"', modes).group(1) in guide
    settings = ' '.join((REPO / 'src/components/AdvancedSettings.vue').read_text(encoding='utf-8').split())
    web = re.search(r'WEB_DESTINATION = "([^"]+)"', settings).group(1)
    assert web.startswith('Searches send your search words to Microsoft Bing') and web in guide
    labels = dict(re.findall(r'(\w+): "([^"]+)"', re.search(r'toolLabels: Record<string, string> = \{([^}]+)\}',
                                                                  settings).group(1)))
    # D95 (Phase 40, 1.6.0): ten switches, the screenshot switch ("capture") added; each label still quoted exactly.
    assert len(labels) == 10 and all(label in guide for label in labels.values()), labels
    scope = 'Local servers must be one self-contained program under 100 MB'
    sentence = re.search(re.escape(scope) + r'[^"<]+', settings).group(0).strip()
    assert sentence.endswith('connect to an HTTP MCP endpoint instead.') and sentence in guide
    shortcuts = (REPO / 'src/shortcuts.ts').read_text(encoding='utf-8')
    for label in re.findall(r'label: "([^"]+)"', shortcuts):
        assert label in guide, label


def test_the_published_gitignore_ignores_no_published_file(real):
    selection, _ = real
    text = (REPO / '.gitignore').read_text(encoding='utf-8')
    gate = pt.gate_gitignore(text, selection['selected'])
    assert gate['status'] == 'passed' and gate['rules'] > 10, gate
    rules = pt.gitignore_rules(text)
    for private in ('.runtime/agent-env/bin/python', 'reports/x.json', 'release/Pebble.dmg', 'models/m.gguf',
                    'node_modules/vue/index.js', 'dist/index.html', 'src-tauri/target/release/pebble',
                    'src-tauri/resources/tokenizer/fb37', 'src-tauri/resources/helpers/pebble-vault',
                    'backend/pebble/__pycache__/app.cpython-314.pyc', 'a/b/.DS_Store', 'logs/backend.log', '.env'):
        assert pt.ignored_by(private, rules), private


def test_the_manifest_is_strict_and_its_patterns_mean_what_they_say(tmp_path):
    assert pt.glob_regex('src/**').match('src/a/b.ts') and not pt.glob_regex('src/**').match('srcx/a')
    assert pt.glob_regex('**/*.gguf').match('models/a.gguf') and pt.glob_regex('**/*.gguf').match('a.gguf')
    assert not pt.glob_regex('docs/HARNESS_PHASE*').match('docs/x/HARNESS_PHASE1.md')
    assert pt.glob_regex('scripts/finalize_*.py').match('scripts/finalize_phase1.py')
    broken = json.loads((REPO / 'publish/manifest.json').read_text())
    for change in (lambda m: m.update(format='other'), lambda m: m['include'].append(3),
                   lambda m: m['never'].append({'glob': 'x'}),
                   lambda m: m['scrub'].append({'path': 'a', 'pattern': '(', 'replace': '', 'count': 1, 'reason': 'r'}),
                   lambda m: m['clean_clone']['provision'].append({'path': 'a', 'sha256': 'abc'})):
        manifest = json.loads(json.dumps(broken))
        change(manifest)
        path = put(tmp_path / ('m%d.json' % len(list(tmp_path.iterdir()))), json.dumps(manifest))
        with pytest.raises(pt.Refused) as error:
            pt.load_manifest(path)
        assert error.value.code == 'manifest_invalid'


# ---- .gitleaks.toml ----------------------------------------------------------------------------------------------


def gitleaks_config():
    return tomllib.loads((REPO / '.gitleaks.toml').read_text(encoding='utf-8'))


def gitleaks_decoded(line):
    """The line as gitleaks also scans it (--max-decode-depth): each base64 run that decodes to printable text, decoded."""
    def decode(match):
        try:
            text = base64.b64decode(match.group(0), validate=True).decode('ascii')
        except (ValueError, UnicodeDecodeError):
            return match.group(0)
        return text if text.isprintable() else match.group(0)
    return re.sub(r'[A-Za-z0-9+/]{16,}={0,2}', decode, line)


def test_gitleaks_keeps_the_default_rules_and_adds_pebbles_own():
    config = gitleaks_config()
    assert config['extend'] == {'useDefault': True}
    rules = {rule['id']: rule for rule in config['rules']}
    assert set(rules) == {'pebble-user-home-path', 'pebble-email-address', 'pebble-account-folder'}
    for rule in rules.values():
        assert '(?<' not in rule['regex'] and '(?=' not in rule['regex'] and '(?!' not in rule['regex']  # RE2
        assert all(keyword == keyword.lower() for keyword in rule['keywords'])

    def finds(rule_id, text):
        """The rule as gitleaks applies it: the regex, then its allowlists on the secret or the match."""
        rule = rules[rule_id]
        hits = []
        for match in re.finditer(rule['regex'], text):
            secret = match.group(rule.get('secretGroup', 0))
            allowed = any(
                any(re.search(pattern, match.group(0) if allow.get('regexTarget') == 'match' else secret)
                    for pattern in allow.get('regexes', ())) and not allow.get('paths')
                for allow in rule.get('allowlists', ()))
            if not allowed:
                hits.append(match.group(0))
        return hits
    assert finds('pebble-user-home-path', 'open ' + USERS + 'janedoe/Projects/x') == [USERS + 'janedoe']
    assert finds('pebble-user-home-path', USERS + 'sam/x and ' + USERS + 'someone-else/y') == []
    assert finds('pebble-email-address', 'mail ' + PERSON) == [PERSON]
    assert finds('pebble-email-address', 'ada' + '@example.test, bob' + '@mail.example.com') == []
    assert finds('pebble-account-folder', '~/' + ACCOUNT_DIR + '/projects/x') == ['/' + ACCOUNT_DIR[:9]]
    assert finds('pebble-account-folder', 'anthropic.claude-3 and claude-opus-5-5') == []
    # The public rules name no account: account labels are private markers (the parent's markers file).
    assert 'claude-[wp]' not in json.dumps(config) and 'claude-[wp]' not in pt.ACCOUNT.pattern
    synthetic = rules['pebble-user-home-path']['allowlists'][0]['regexes'][0]
    assert synthetic == '^(%s)$' % '|'.join(MANIFEST['scan']['synthetic_user_names'])


def test_gitleaks_allowlists_pin_the_ten_triaged_findings_to_their_files_and_lines():
    """i2-08: 0 real secrets in 10 hits; the Phase 25 publish probe: 0 real secrets in 9 more (synthetic values the
    Phase 24 tracks added after i2-08). Each allowlist names one file and its exact line, never a line number, and
    still matches today's file, so none is stale."""
    config = gitleaks_config()
    config_text = (REPO / '.gitleaks.toml').read_text(encoding='utf-8')
    allowlists = config['allowlists']
    assert len(allowlists) == 13  # 2 + 1 + 6 line-pinned files, 4 more from the Phase 25 probe, and the DMG
    pinned = 0
    for allow in allowlists[:-1]:
        assert allow['condition'] == 'AND' and allow['regexTarget'] == 'line' and len(allow['paths']) == 1
        # Phase 46 (2026-10-28): gitleaks 8.30 skips every file a global allowlist's paths match unless the allowlist
        # names its rules, so each names the one rule its line trips; and its line target begins with the newline
        # before the line, so each pattern anchors with (?m).
        assert allow['targetRules'] in (['generic-api-key'], ['aws-access-token'])
        assert all(pattern.startswith('(?m)^') for pattern in allow['regexes'])
        rel = re.fullmatch(r'\(\^\|/\)(.+)\$', allow['paths'][0]).group(1).replace('\\.', '.')
        lines = (REPO / rel).read_text(encoding='utf-8').splitlines()
        for pattern in allow['regexes']:
            matching = [line for line in lines if re.search(pattern, line) or re.search(pattern, gitleaks_decoded(line))]
            assert matching, (rel, pattern)
            pinned += len(matching)
            # The config is scanned too: it pins each line's shape and never repeats a value.
            for line in matching:
                for value in re.findall(r'''['"]([^'"]{10,})['"]''', line):
                    for part in value.split():  # each space-free value in the quoted text
                        assert len(part) < 10 or part not in config_text, (rel, part[:12])
    # the three zephyr canaries share one rule; i2-08 flagged one of them; Phase 25 adds 7 lines; Phase 46 pins the
    # decoded view of the scrubber shapes' JSON line in its two files
    assert pinned == 20
    assert allowlists[-1]['paths'] == ['''(^|/)release/Pebble_0\\.4\\.0_arm64\\.dmg$''']
    # The reviewed e-mail list is the manifest's: every address matches a path-bound allowlist and no other.
    email = next(rule for rule in config['rules'] if rule['id'] == 'pebble-email-address')
    bound = [allow for allow in email['allowlists'] if allow.get('condition') == 'AND']
    for entry in MANIFEST['scan']['allowed_emails']:
        matching = [allow for allow in bound if any(re.search(p, entry['address']) for p in allow['regexes'])]
        assert len(matching) == 1, entry['address']
        for path in entry['paths']:
            assert re.search(matching[0]['paths'][0], path), (entry['address'], path)
    licences = [allow for allow in email['allowlists'] if allow.get('paths') and not allow.get('regexes')]
    assert [allow['paths'] for allow in licences] == [['''(^|/)src-tauri/resources/licenses/''']]


# ---- publish() on a small tree ------------------------------------------------------------------------------------


SMALL_MANIFEST = {
    'format': pt.FORMAT, 'repository': 'https://github.com/tohin003/Pebble', 'branch': 'main', 'about': 'test',
    'prune': ['.runtime', 'reports', 'node_modules'],
    'include': ['README.md', '.gitignore', '.gitleaks.toml', 'install.sh', 'package.json', 'package-lock.json',
                'src/**', 'backend/**', 'scripts/**', 'docs/USER_GUIDE.md', 'src-tauri/Cargo.lock',
                'src-tauri/resources/NOTICE.md', 'src-tauri/resources/LICENSE', 'src-tauri/resources/AGENTGUARD-LICENSE',
                'src-tauri/resources/licenses/**'],
    'exclude': [{'glob': 'scripts/finalize_*.py', 'reason': 'D11'}, {'glob': '**/__pycache__/**', 'reason': 'cache'}],
    'never': [{'glob': '.runtime/**', 'reason': 'private'}, {'glob': 'docs/CLAUDE_HANDOFF_*', 'reason': 'D11'},
              {'glob': '**/*.gguf', 'reason': 'weights'}],
    'scrub': [{'path': 'scripts/audit.py', 'pattern': r'\\\.claude-[a-z]+\|', 'replace': r'\.claude-[^/]+|',
               'count': 1, 'reason': 'D11'}],
    'scan': {'synthetic_user_names': ['sam', 'x'], 'reserved_email_domains': ['example', 'test', 'invalid'],
             'allowed_emails': [{'address': 'i' + '@izs.me', 'paths': ['package-lock.json'], 'reason': 'npm text'}],
             'allowed_email_paths': [{'glob': 'src-tauri/resources/licenses/**', 'reason': 'verbatim'}]},
    'clean_clone': {'provision': [], 'deselect': []},
}
TOKENIZER = b'vocabulary bytes'


class Tree:
    """A small checkout with one of everything the manifest must keep out, and its gate inputs."""

    def __init__(self, tmp_path, manifest=None):
        self.tmp = tmp_path
        self.source = tmp_path / 'checkout'
        self.manifest = json.loads(json.dumps(manifest or SMALL_MANIFEST))
        s = self.source
        put(s / 'README.md', '# Pebble\nAsk ada' + '@example.test, never a person.\n')
        put(s / '.gitignore', '/.runtime/\n/reports/\n/node_modules/\n__pycache__/\n*.gguf\n')
        put(s / '.gitleaks.toml', 'title = "test"\n')
        put(s / 'install.sh', '#!/bin/bash\nset -euo pipefail\necho hi\n', 0o755)
        put(s / 'package.json', json.dumps({'name': 'pebble-desktop', 'version': '1.0.0'}))
        put(s / 'package-lock.json', '{"deprecated": "contact i' + '@izs.me"}\n')
        put(s / 'src/App.vue', '<template>hi</template>\n')
        put(s / 'src/App.test.mjs', "import test from 'node:test';\ntest('ok', () => {});\n")
        put(s / 'backend/pebble/app.py', "HOME_EXAMPLE = '" + USERS + "sam/Projects'\n")
        put(s / 'backend/tests/pytest.ini', '[pytest]\naddopts = -p no:cacheprovider\n')
        put(s / 'backend/tests/test_small.py', 'def test_ok():\n    assert True\n')
        put(s / 'backend/pebble/__pycache__/app.cpython-314.pyc', b'\x00compiled')
        put(s / 'scripts/audit.py', "PRIVATE = r'(vault|\\" + ACCOUNT_DIR + "|x)'\n")
        put(s / 'scripts/finalize_phase1.py', "ACCOUNTS = '~/" + ACCOUNT_DIR + "'\n")
        put(s / 'src-tauri/Cargo.lock', 'version = 4\n')
        put(s / 'src-tauri/resources/NOTICE.md', 'Pebble\nCopyright 2026 Tohin Majumder\n')
        put(s / 'src-tauri/resources/LICENSE', 'Apache License\n')
        put(s / 'src-tauri/resources/AGENTGUARD-LICENSE', 'Apache License\n')
        put(s / 'src-tauri/resources/licenses/x-LICENSE', 'Copyright (c) Upstream Author <dev' + '@upstream.org>\n')
        put(s / 'src-tauri/resources/tokenizer/vocab', TOKENIZER)
        put(s / 'docs/USER_GUIDE.md', '# Guide\n')
        put(s / 'docs/CLAUDE_HANDOFF_CONTEXT.md', 'private ' + PERSON + '\n')
        put(s / '.runtime/notes.md', 'private ' + USERS + 'janedoe/x\n')
        put(s / 'reports/r.json', '{"path": "' + USERS + 'janedoe/x"}\n')
        put(s / 'models/m.gguf', b'GGUF')
        put(s / 'node_modules/vue/index.js', 'export default 1\n')
        self.markers = put(tmp_path / 'private-markers.txt', '# private strings\n' + FOLDER_MARKER + '\n')
        self.out, self.report = tmp_path / 'public', tmp_path / 'publish-report.json'
        self.calls = []
        self.write_manifest()

    def write_manifest(self):
        put(self.source / 'publish/manifest.json', json.dumps(self.manifest))

    def inventory(self, **override):
        s = self.source
        report = {'format': pt.INVENTORY_FORMAT, 'result': 'complete', 'summary': {'findings': {}},
                  'inputs': {'package_lock': {'sha256': sha((s / 'package-lock.json').read_bytes())},
                             'cargo_lock': {'sha256': sha((s / 'src-tauri/Cargo.lock').read_bytes())},
                             'requirements': {'sha256': sha(b'pydantic==2\n')},
                             'licenses': {'files': {'x-LICENSE': sha((s / 'src-tauri/resources/licenses/x-LICENSE')
                                                                    .read_bytes())}},
                             'resource_notices': {name: sha((s / 'src-tauri/resources' / name).read_bytes())
                                                  for name in pt.RESOURCE_NOTICES}}}
        put(s / 'backend/requirements.txt', 'pydantic==2\n')
        report.update(override)
        return put(self.tmp / 'inventory.json', json.dumps(report))

    def runner(self, gitleaks_findings=(), failing=()):
        def run(argv, cwd=None, env=None, timeout=None):
            self.calls.append({'argv': list(argv), 'cwd': cwd, 'env': env})
            if argv[0] == 'gitleaks':
                Path(argv[argv.index('--report-path') + 1]).write_text(json.dumps(list(gitleaks_findings)))
                return (1 if gitleaks_findings else 0), ''
            if any(rel in argv for rel in failing):
                return 1, 'FAILED test\n1 failed in 0.01s\n'
            if '-m' in argv and 'pytest' in argv:
                return 0, '.\n1 passed in 0.01s\n'
            if '--test' in argv:
                return 0, 'ℹ tests 1\nℹ pass 1\nℹ fail 0\n'
            return 0, ''
        return run

    def publish(self, *, runner=None, **options):
        defaults = dict(markers_file=self.markers, gitleaks='gitleaks', shellcheck='shellcheck', python='python3',
                        node='node', node_modules=str(self.source / 'node_modules'), scratch=self.tmp / 'scratch')
        defaults.update(options)
        if 'inventory_report' not in defaults:
            defaults['inventory_report'] = self.inventory()
        return pt.publish(self.source, self.out, self.report, runner=runner or self.runner(), **defaults)


def gate_status(report):
    return {name: gate['status'] for name, gate in report['gates'].items()}


def test_a_clean_tree_is_publishable_and_holds_exactly_the_allowlist(tmp_path):
    tree = Tree(tmp_path)
    report, code = tree.publish()
    assert code == 0 and report['result'] == 'publishable', gate_status(report)
    published = sorted(p.relative_to(tree.out).as_posix() for p in tree.out.rglob('*') if p.is_file())
    assert published == sorted(['.gitignore', '.gitleaks.toml', 'README.md', 'backend/pebble/app.py',
                                'backend/requirements.txt', 'backend/tests/pytest.ini', 'backend/tests/test_small.py',
                                'docs/USER_GUIDE.md', 'install.sh', 'package-lock.json', 'package.json',
                                'scripts/audit.py', 'src-tauri/Cargo.lock', 'src-tauri/resources/AGENTGUARD-LICENSE',
                                'src-tauri/resources/LICENSE', 'src-tauri/resources/NOTICE.md',
                                'src-tauri/resources/licenses/x-LICENSE', 'src/App.test.mjs', 'src/App.vue'])
    # Excluded and never-published paths are absent; the private checkout is unchanged.
    for absent in ('.runtime', 'reports', 'models', 'node_modules', 'docs/CLAUDE_HANDOFF_CONTEXT.md',
                   'scripts/finalize_phase1.py', 'backend/pebble/__pycache__', 'src-tauri/resources/tokenizer'):
        assert not (tree.out / absent).exists(), absent
    assert ACCOUNT_DIR in (tree.source / 'scripts/audit.py').read_text()
    assert (tree.out / 'scripts/audit.py').read_text() == "PRIVATE = r'(vault|\\.claude-[^/]+|x)'\n"
    assert stat.S_IMODE((tree.out / 'install.sh').stat().st_mode) == 0o755
    assert stat.S_IMODE((tree.out / 'README.md').stat().st_mode) == 0o644
    assert report['file_count'] == 19 and report['tree_sha256'] == pt.tree_digest(report['files'])
    assert report['selection']['excluded']['scripts/finalize_*.py']['paths'] == ['scripts/finalize_phase1.py']
    assert set(gate_status(report).values()) == {'passed'}
    assert report['gates']['private_markers']['markers'] == 1
    # The gates' commands: gitleaks with the tree's own config, bash -n and shellcheck on install.sh, and each test
    # file on its own in the clean clone, with its own basetemp and a new HOME.
    argvs = [call['argv'] for call in tree.calls]
    assert argvs[0] == ['gitleaks', 'dir', str(tree.out), '--config', str(tree.out / '.gitleaks.toml'),
                        '--report-format', 'json', '--report-path', str(tree.report) + '.gitleaks.json', '--redact',
                        '--no-banner', '--exit-code', '1']
    assert argvs[1] == ['/bin/bash', '-n', str(tree.out / 'install.sh')]
    assert argvs[2] == ['shellcheck', '--shell=bash', '--severity=style', 'install.sh']
    clone = tmp_path / 'scratch' / 'clone'
    pytest_call = tree.calls[3]
    assert pytest_call['argv'][:6] == ['python3', '-m', 'pytest', '-c', 'backend/tests/pytest.ini',
                                       'backend/tests/test_small.py']
    assert pytest_call['argv'][-2:] == ['--basetemp', str(tmp_path / 'scratch' / 'pytest' / '0001')]
    assert pytest_call['cwd'] == str(clone) and pytest_call['env']['HOME'] == str(tmp_path / 'scratch' / 'home')
    assert (tmp_path / 'scratch' / 'pytest').is_dir()  # pytest does not create a --basetemp's parent itself
    assert tree.calls[4]['argv'] == ['node', '--test', '--test-concurrency=1', 'src/App.test.mjs'] and tree.calls[4]['cwd'] == str(clone)
    assert os.path.islink(clone / 'node_modules') and not (clone / '.runtime').exists()
    # Nothing is committed, pushed or uploaded: the commands are only printed.
    assert not any(argv[0] in ('git', 'gh', 'curl') for argv in argvs)
    commands = report['parent_commands']
    assert commands[1] == 'git init --initial-branch=main' and 'git push -u origin main' in commands
    assert any("-m 'Pebble 1.0.0'" in line for line in commands) and 'git tag -a v1.0.0' in ' '.join(commands)
    saved = json.loads(tree.report.read_text())
    assert saved['result'] == 'publishable' and stat.S_IMODE(tree.report.stat().st_mode) == 0o600


@pytest.mark.parametrize('planted, gate, expected', [
    ("WHERE = '" + USERS + "janedoe/Projects/pebble'\n", 'machine_paths', USERS + 'janedoe'),
    ('OWNER = "' + PERSON + '"\n', 'emails', PERSON),
    ("SESSIONS = '~/" + ACCOUNT_DIR + "/projects/abc'\n", 'account_folders', ACCOUNT_DIR[:9]),
])
def test_a_planted_marker_makes_it_refuse(tmp_path, planted, gate, expected):
    tree = Tree(tmp_path)
    put(tree.source / 'backend/pebble/leak.py', '"""A module."""\n' + planted)
    report, code = tree.publish()
    assert code == 3 and report['result'] == 'refused'
    found = report['gates'][gate]
    assert found['status'] == 'failed' and found['hits'] == 1
    item = found['items'][0]
    assert item['path'] == 'backend/pebble/leak.py' and item['line'] == 2
    assert expected in (item.get('text') or item.get('address'))
    assert report['gates']['clean_clone_tests'] == {'status': 'not_run', 'reason': 'earlier_gate_failed'}
    assert 'parent_commands' not in report


def test_this_macs_own_paths_and_the_private_markers_refuse_without_echoing_a_marker(tmp_path, monkeypatch):
    tree = Tree(tmp_path)
    home = tmp_path / 'person-home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    put(tree.source / 'docs/USER_GUIDE.md', '# Guide\nBuilt in %s/Projects.\nFrom the %s folder.\n'
        % (home, FOLDER_MARKER.upper()))
    report, code = tree.publish()
    assert code == 3
    machine = report['gates']['machine_paths']
    assert machine['status'] == 'failed' and {i['rule'] for i in machine['items']} == {'this_mac'}
    private = report['gates']['private_markers']
    assert private['status'] == 'failed' and private['items'] == [
        {'path': 'docs/USER_GUIDE.md', 'line': 3, 'marker_line': 2}]
    assert 'qwen' not in tree.report.read_text().lower()  # a marker is named by its line, never by its text


def test_markers_are_required_and_never_published(tmp_path):
    tree = Tree(tmp_path)
    tree.markers.unlink()
    with pytest.raises(pt.Refused) as error:
        tree.publish()
    assert error.value.code == 'markers_file_unreadable' and not tree.out.exists()
    report, code = tree.publish(markers_file=None)  # the default under .runtime/ does not exist here
    assert code == 5 and report['gates']['private_markers']['reason'] == 'markers_file_missing'


@pytest.mark.parametrize('arrange, code', [
    (lambda tree: tree.manifest['include'].append('docs/**'), 'never_published_present'),
    (lambda tree: os.symlink('/etc/hosts', tree.source / 'src/hosts.txt'), 'link_selected'),
    (lambda tree: os.symlink(tree.source / 'docs', tree.source / 'src/docs'), 'link_selected'),
])
def test_a_never_path_or_a_link_in_the_selection_refuses_before_anything_is_written(tmp_path, arrange, code):
    tree = Tree(tmp_path)
    arrange(tree)
    tree.write_manifest()
    report, exit_code = tree.publish()
    assert exit_code == 3 and report['refused'] == 'selection' and not tree.out.exists()
    assert code in {problem['code'] for problem in report['selection']['problems']}
    assert tree.calls == []


def test_links_to_unpublished_files_are_reported_for_review_but_do_not_refuse(tmp_path):
    tree = Tree(tmp_path)
    put(tree.source / 'docs/USER_GUIDE.md', '# Guide\nSee [the handoff](CLAUDE_HANDOFF_CONTEXT.md), [the readme]'
                                            '(../README.md#pebble), [code](../src/) and [a site](https://example.test/).\n')
    report, code = tree.publish()
    assert code == 0 and report['dangling_links']['count'] == 1
    assert report['dangling_links']['items'] == [{'path': 'docs/USER_GUIDE.md', 'target': 'CLAUDE_HANDOFF_CONTEXT.md'}]


def test_a_scrub_that_no_longer_matches_refuses(tmp_path):
    tree = Tree(tmp_path)
    put(tree.source / 'scripts/audit.py', "PRIVATE = r'(vault|x)'\n")
    report, code = tree.publish()
    assert code == 3 and report['refused'] == 'scrub_count_differs'
    assert report['detail'] == {'path': 'scripts/audit.py', 'expected': 1, 'found': 0}


def test_gitleaks_findings_shellcheck_failures_and_missing_tools_are_never_a_pass(tmp_path, monkeypatch):
    finding = [{'File': str(tmp_path / 'public/README.md'), 'StartLine': 2, 'RuleID': 'generic-api-key',
                'Fingerprint': 'README.md:generic-api-key:2', 'Secret': 'REDACTED'}]
    tree = Tree(tmp_path / 'a')
    report, code = tree.publish(runner=tree.runner(gitleaks_findings=finding))
    assert code == 3 and report['gates']['gitleaks']['status'] == 'failed'
    assert report['gates']['gitleaks']['items'] == [{'path': finding[0]['File'], 'line': 2, 'rule': 'generic-api-key',
                                                     'fingerprint': 'README.md:generic-api-key:2'}]
    tree = Tree(tmp_path / 'b')
    report, code = tree.publish(runner=tree.runner(failing=('install.sh',)))
    assert code == 3 and report['gates']['shellcheck']['status'] == 'failed'
    tree = Tree(tmp_path / 'c')
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    report, code = tree.publish(gitleaks=None, shellcheck=None)
    assert code == 5 and report['gates']['gitleaks']['reason'] == 'tool_missing'
    assert report['gates']['shellcheck']['reason'] == 'tool_missing'


def test_the_inventory_must_be_complete_and_made_from_the_published_files(tmp_path):
    tree = Tree(tmp_path)
    report, code = tree.publish(inventory_report=None)
    assert code == 5 and report['gates']['inventory']['reason'] == 'no_inventory_report'
    for name, override in (('gaps', {'result': 'gaps'}), ('lock', {'inputs': {'package_lock': {'sha256': '0' * 64}}})):
        tree = Tree(tmp_path / name)
        report, code = tree.publish(inventory_report=tree.inventory(**override))
        assert code == 3 and report['gates']['inventory']['status'] == 'failed'
    assert report['gates']['inventory']['reason'] == 'inventory_differs_from_tree'


def test_a_gitignore_that_hides_a_published_file_refuses(tmp_path):
    tree = Tree(tmp_path)
    put(tree.source / '.gitignore', '/.runtime/\n*.vue\n')
    report, code = tree.publish()
    assert code == 3 and report['gates']['gitignore']['ignored_published_files'] == [
        {'path': 'src/App.vue', 'rule': '*.vue'}]
    tree = Tree(tmp_path / 'negated')
    put(tree.source / '.gitignore', '*.md\n!README.md\n')
    report, code = tree.publish()
    assert report['gates']['gitignore']['reason'] == 'gitignore_syntax_unsupported'


def test_clean_clone_tests_fail_partial_and_provisioning(tmp_path):
    tree = Tree(tmp_path / 'fail')
    report, code = tree.publish(runner=tree.runner(failing=('backend/tests/test_small.py',)))
    assert code == 3 and report['gates']['clean_clone_tests']['failed'][0]['file'] == 'backend/tests/test_small.py'
    tree = Tree(tmp_path / 'partial')
    report, code = tree.publish(test_files=['src/App.test.mjs'])
    assert code == 5 and report['gates']['clean_clone_tests']['status'] == 'partial'
    assert report['gates']['clean_clone_tests']['python_files'] == 0
    manifest = json.loads(json.dumps(SMALL_MANIFEST))
    manifest['clean_clone']['provision'] = [{'path': 'src-tauri/resources/tokenizer/vocab', 'sha256': sha(TOKENIZER),
                                             'reason': 'fetched once'}]
    tree = Tree(tmp_path / 'provisioned', manifest)
    report, code = tree.publish()
    assert code == 0, gate_status(report)
    clone = tmp_path / 'provisioned' / 'scratch' / 'clone'
    assert (clone / 'src-tauri/resources/tokenizer/vocab').read_bytes() == TOKENIZER
    assert not (tree.out / 'src-tauri/resources/tokenizer').exists()  # provisioned in the clone, never published
    put(tree.source / 'src-tauri/resources/tokenizer/vocab', b'changed')
    tree.out, tree.report = tmp_path / 'provisioned' / 'public2', tmp_path / 'provisioned' / 'report2.json'
    report, code = tree.publish(scratch=tmp_path / 'provisioned' / 'scratch2')
    assert code == 3 and report['gates']['clean_clone_tests']['reason'] == 'provision_differs'


def test_it_refuses_an_existing_tree_a_tree_inside_the_checkout_and_a_report_inside_the_tree(tmp_path):
    tree = Tree(tmp_path)
    tree.out.mkdir()
    with pytest.raises(pt.Refused) as error:
        tree.publish()
    assert error.value.code == 'out_exists'
    tree.out = tree.source / 'public'
    with pytest.raises(pt.Refused) as error:
        tree.publish()
    assert error.value.code == 'out_inside_checkout'
    tree.out = tree.source / '.runtime' / 'public'  # the private working folder is never selected
    report, code = tree.publish()
    assert code == 0, gate_status(report)
    tree.out, tree.report = tmp_path / 'p2', tmp_path / 'p2' / 'report.json'
    with pytest.raises(pt.Refused) as error:
        tree.publish(scratch=tmp_path / 'scratch-2')
    assert error.value.code == 'report_exists_or_inside_tree'


def test_end_to_end_with_fake_scanners_the_real_runner_and_real_test_runs(tmp_path, monkeypatch):
    """The CLI, with fake gitleaks and shellcheck first in PATH, a real pytest on the clone's test file and a real
    node --test on its .test.mjs."""
    node = shutil.which('node')
    tree = Tree(tmp_path)
    fakes = tmp_path / 'bin'
    log = tmp_path / 'scanner-calls.jsonl'
    impl = put(tmp_path / 'fake_scanner.py',
               'import json, sys\n'
               'tool, args = sys.argv[1], sys.argv[2:]\n'
               'open(%r, "a").write(json.dumps({"tool": tool, "argv": args}) + "\\n")\n'
               'if tool == "gitleaks":\n'
               '    open(args[args.index("--report-path") + 1], "w").write("[]")\n' % str(log))
    quote = lambda value: "'" + str(value).replace("'", "'\\''") + "'"  # noqa: E731 (the paths may hold spaces)
    for tool in ('gitleaks', 'shellcheck'):  # the scanners get only PATH, HOME and LANG, so the shim names its paths
        put(fakes / tool, '#!/bin/bash\nexec %s %s %s "$@"\n' % (quote(sys.executable), quote(impl), tool), 0o755)
    monkeypatch.setenv('PATH', '%s:%s' % (fakes, os.environ.get('PATH', '/usr/bin:/bin')))
    modules = tmp_path / 'node_modules'
    modules.mkdir()
    argv = ['--source', str(tree.source), '--out', str(tree.out), '--report', str(tree.report),
            '--markers-file', str(tree.markers), '--inventory-report', str(tree.inventory()),
            '--python', sys.executable, '--node-modules', str(modules), '--scratch', str(tmp_path / 'scratch')]
    if node:
        argv += ['--node', node]
    code = pt.main(argv)
    report = json.loads(tree.report.read_text())
    tests = report['gates']['clean_clone_tests']
    if not node:
        assert code == 3 and tests['results'][-1]['last_line'] == 'node or node_modules missing'
        pytest.skip('node is not on PATH here; the Python half ran.')
    assert code == 0, gate_status(report)
    assert [r['file'] for r in tests['results']] == ['backend/tests/test_small.py', 'node --test (1 files)']
    assert tests['results'][0]['last_line'].startswith('1 passed')
    assert 'pass 1' in tests['results'][1]['last_line']
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call['tool'] for call in calls] == ['gitleaks', 'shellcheck']
    assert calls[0]['argv'][:2] == ['dir', str(tree.out)]
