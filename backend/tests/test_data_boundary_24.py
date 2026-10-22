"""Phase 24 (t24-data): the data boundary of the Documents copies, what reading them back may do, and the copy pass.

- V1-003: every copies folder (the default ~/Documents/Pebble, a folder settings.json names, and every earlier one) is
  a protected root. A Workspace refuses it as a project, and a project that holds it (~/Documents) refuses paths into
  it, so Pebble's own tools can neither read a copy nor delete one. The person's own storage choice is unaffected.
- V1-004: a Memory file is read back only for deletions that are certain: an entry that was in the version of the file
  the person saved (its pebble:memory-version line), gone on two passes CONFIRM_SECONDS apart. An editor saving an older
  copy forgets nothing added since; more than three entries, or a whole layer, wait for the person's confirmation.
- V1-036: a PEBBLE.md over 4,000 characters is used up to its last full line within the limit ('truncated'), never the
  previous version.
- V1-019, V1-062: file names and index titles are scrubbed; the scrubber removes ten more secret shapes (best effort).
- V1-011: the copy pass reads one grouped query through the read connection and skips what did not change.
- V1-056, V1-058, V1-060, V1-101, V1-017: the storage page's rows outside both folders, the one-time iCloud event and log
  tails kept out of iCloud, the PEBBLE.md and settings.json buttons, and removing the old sign-in copies.
Under a fake HOME with an injected clock, opener and editor. No network, model call or Keychain.
"""

import inspect
import json
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from pebble import data_home, data_mirror, settings_file
from pebble.app import App
from pebble.data_home import resolve
from pebble.data_mirror import DataServices, scrub
from pebble.policy import PolicyError, Workspace

KEY = 'sk-' + 'proj' + 'Q9x8W7v6U5t4S3r2Q1p0O9n8M7'
CONFIRM = getattr(data_mirror, 'CONFIRM_SECONDS', 60.0)  # read softly: these tests also ran against 0.6.1's module
OLD = ("SELECT c.id,c.title,c.updated,"
       "(SELECT count(*) FROM messages m WHERE m.chat_id=c.id AND m.role IN ('user','assistant')) AS n,"
       "(SELECT min(created) FROM messages m WHERE m.chat_id=c.id) AS first,"
       "(SELECT max(created) FROM messages m WHERE m.chat_id=c.id) AS last FROM chats c")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    monkeypatch.setenv('AGENTGUARD_HOME', str(tmp_path / 'guard-unused'))
    data_home.deactivate()
    yield home
    data_home.deactivate()


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def make_services(app, **options):
    """DataServices with the options this version takes (the tests also ran against 0.6.1's module)."""
    accepted = inspect.signature(DataServices).parameters
    return DataServices(app, **{k: v for k, v in options.items() if k in accepted})


def make_env(home, default=False, vault=None):
    layout = resolve(None, env={}) if default else resolve(str(home / '.pebble'), None, env={})
    data_home.ensure_layout(layout)
    report = data_home.migrate(layout)
    data_home.activate(layout, Path(report['data']), report)
    events, opened, edited, clock = [], [], [], Clock()
    original = DataServices.start
    DataServices.start = lambda self: None  # these tests drive their own instance, as test_data_mirror_22 does
    try:
        app = App(Path(report['data']), home / 'resources', lambda *event: events.append(event))
    finally:
        DataServices.start = original
    gaps, during = [], []

    def nap(seconds):  # the CONFIRM_GAP wait of a pass the person started; `during` changes files within it
        gaps.append(seconds)
        if during:
            during.pop(0)()
    services = make_services(app, opener=opened.append, editor=edited.append, clock=clock, vault=vault, sleep=nap)
    return type('Env', (), {'app': app, 'services': services, 'docs': home / 'Documents' / 'Pebble', 'events': events,
                            'opened': opened, 'edited': edited, 'clock': clock, 'root': home / '.pebble', 'home': home,
                            'layout': layout, 'report': report, 'gaps': gaps, 'during': during})


@pytest.fixture
def env(fake_home):
    value = make_env(fake_home)
    yield value
    value.services.stop()
    value.app.close()


def chat(app, text='Plan a trip to Lisbon', reply='Here is a plan.'):
    cid = app.store.new_chat()
    app.store.message(cid, 'user', text)
    app.store.message(cid, 'assistant', reply)
    return cid


# The background poll's pass (two passes CONFIRM_SECONDS apart); 0.6.1's tick() has no such argument.
BACKGROUND = {'background': True} if 'background' in inspect.signature(DataServices.tick).parameters else {}


def again(env, seconds=CONFIRM):
    """A background poll pass, `seconds` after the last one."""
    env.clock.now += seconds
    return env.services.tick(**BACKGROUND)


def refresh(env):
    """A pass the person started (Update copies now): confirms within the pass, CONFIRM_GAP apart."""
    return env.services.methods()['storage_refresh']()


def facts(env):
    return env.docs / 'Memory' / 'Facts & knowledge.md'


def remove_block(text, ident):
    lines = text.split('\n')
    end = lines.index(f'<!-- pebble:memory id={ident} -->')
    start = max(i for i in range(end) if lines[i].startswith('## '))
    return '\n'.join(lines[:start] + lines[end + 1:])


def memory_ids(env, kind='semantic'):
    return {r['id'] for r in env.app.store.query('SELECT id FROM memories WHERE kind=?', (kind,))}


# ---- V1-003: the copies are outside Pebble's own tool boundary ---------------------------------------------------------

def test_documents_copies_are_outside_the_tool_boundary(env):
    cid = chat(env.app)
    keep = env.app.memory.put('semantic', 'The locker code is 4417.')
    env.services.tick()
    assert (env.docs / 'Conversations').is_dir() and facts(env).is_file()
    documents = Workspace(env.home / 'Documents')  # a project at ~/Documents stays possible
    for name in ('Pebble/Memory/x.md', 'Pebble/Memory/Facts & knowledge.md', 'Pebble/Conversations/README.md',
                 'pebble/memory/Facts & knowledge.md'):
        with pytest.raises(PolicyError, match="Pebble's own data folders"):
            documents.path(name, create=True)
    with pytest.raises(PolicyError):
        documents.read('Pebble/Memory/Facts & knowledge.md')
    (env.home / 'Documents' / 'notes.md').write_text('mine')
    listed = documents.files()
    assert 'notes.md' in listed and not any(n.startswith('Pebble') for n in listed), 'no copy is listed to a tool'
    for folder in (env.docs, env.docs / 'Memory', env.docs / 'Conversations'):
        with pytest.raises(PolicyError, match="Pebble's own data folders"):
            Workspace(folder)
    assert data_home.tool_refused(env.docs / 'Memory' / 'Facts & knowledge.md')
    assert data_home.tool_refused(str(env.docs).replace('/Pebble', '/PEBBLE')), 'compared case-insensitively'
    assert data_home.protected_paths_below(env.home / 'Documents') == [Path(os.path.realpath(env.docs))]
    # The person's own choice is unaffected: ~/Documents/Pebble is still a valid storage folder in settings.json.
    assert not data_home.inside_protected(env.docs)
    assert settings_file.validate_storage({'documents_mirror': True, 'documents_folder': str(env.docs)})
    # A tool edit there never happened, so nothing was forgotten or trashed.
    assert keep in memory_ids(env) and env.app.store.trashed() == {} and cid


def test_the_default_copies_folder_is_protected_without_a_storage_service(fake_home, tmp_path):
    (fake_home / 'Documents' / 'Pebble' / 'Memory').mkdir(parents=True)
    with pytest.raises(PolicyError):
        Workspace(fake_home / 'Documents' / 'Pebble')
    with pytest.raises(PolicyError):
        Workspace(fake_home / 'Documents').path('Pebble/Memory/Facts & knowledge.md', create=True)
    project = tmp_path / 'project'
    project.mkdir()
    assert Workspace(project).path('notes.md', create=True), 'other projects are unchanged'


def test_a_custom_and_a_retired_copies_folder_stay_protected(env):
    chat(env.app)
    env.services.tick()
    elsewhere, later = env.home / 'Notes' / 'Pebble copies', env.home / 'Archive' / 'Pebble'
    env.services.methods()['storage_set'](documents_folder=str(elsewhere))
    assert (elsewhere / 'Conversations').is_dir()
    with pytest.raises(PolicyError):
        Workspace(elsewhere)
    with pytest.raises(PolicyError):
        Workspace(env.home / 'Notes').path('Pebble copies/Conversations/x.md', create=True)
    env.services.methods()['storage_set'](documents_folder=str(later))
    for folder in (elsewhere, later, env.docs):
        assert data_home.tool_refused(folder / 'Conversations' / 'x.md'), folder
    # After a restart (a new process), the retired folders come back from the index and stay protected.
    data_home.deactivate()
    assert not data_home.tool_refused(elsewhere / 'x.md'), 'registrations live in this process only'
    data_home.activate(env.layout, Path(env.report['data']), env.report)
    DataServices(env.app, opener=env.opened.append, clock=env.clock)
    assert data_home.tool_refused(elsewhere / 'x.md') and data_home.tool_refused(later / 'x.md')
    retired = {r['root'] for r in env.services.manifest['retired']}
    assert {str(env.docs), str(elsewhere)} <= retired


# ---- V1-004: what a Memory file may make Pebble forget --------------------------------------------------------------

def test_an_editor_saving_an_older_copy_forgets_nothing_added_since(env):
    first = env.app.memory.put('semantic', 'Lisbon is the capital of Portugal.')
    env.services.tick()
    opened_in_editor = facts(env).read_text()  # the editor loads this version
    learned = env.app.memory.put('semantic', 'Porto is in the north.')
    env.services.tick()
    assert f'id={learned}' in facts(env).read_text(), 'Pebble rewrote the copy with the new entry'
    facts(env).write_text(opened_in_editor)  # the editor saves its older text (autosave)
    for seconds in (0, 30, 61, 120):
        again(env, seconds)
    assert {first, learned} <= memory_ids(env), 'an older copy forgets nothing'
    assert not env.app.store.query('SELECT 1 FROM memory_tombstones')
    assert f'id={learned}' in facts(env).read_text(), 'the copy is complete again'
    assert '<!-- pebble:memory-version ' in opened_in_editor, 'each copy says which version it is'
    # In an older copy, removing an entry that copy did show is the person's own deletion.
    older = facts(env).read_text()
    newest = env.app.memory.put('semantic', 'Coimbra has an old university.')
    env.services.tick()
    facts(env).write_text(remove_block(older, first))
    again(env, 0)
    assert first in memory_ids(env), 'one background pass is not enough'
    again(env)
    assert first not in memory_ids(env) and {learned, newest} <= memory_ids(env)
    assert env.app.store.query('SELECT 1 FROM memory_tombstones'), 'a deliberate removal leaves its tombstone'


def test_a_one_pass_absence_forgets_nothing(env):
    one = env.app.memory.put('semantic', 'The office opens at nine.')
    two = env.app.memory.put('semantic', 'The office closes at six.')
    env.services.tick()
    text = facts(env).read_text()
    facts(env).write_text(remove_block(text, one))
    again(env, 0)
    again(env, 30)
    assert one in memory_ids(env), 'thirty seconds later it still waits'
    assert f'id={one}' not in facts(env).read_text(), 'the person sees their own file while Pebble waits'
    facts(env).write_text(text)  # back (a sync tool restored it, or the person undid it)
    again(env, 40)
    again(env, 120)
    assert {one, two} <= memory_ids(env)
    assert env.services.manifest['absent']['memory'] == {}
    # The whole file gone for one pass (an atomic save, a sync rename) and back: nothing.
    os.rename(facts(env), facts(env).with_name('moved.md'))
    again(env, 0)
    os.rename(facts(env).with_name('moved.md'), facts(env))
    again(env, 120)
    assert {one, two} <= memory_ids(env) and env.services.summary()['documents']['confirm'] == []


def test_forgetting_more_than_a_few_entries_or_a_whole_layer_waits_for_the_person(env):
    ids = [env.app.memory.put('semantic', f'Fact number {n} about the harbour.') for n in range(5)]
    env.services.tick()
    text = facts(env).read_text()
    for ident in ids[:4]:
        text = remove_block(text, ident)
    facts(env).write_text(text)
    again(env, 0)
    result = again(env)
    assert result['reconcile']['forgotten'] == 0 and set(ids) <= memory_ids(env)
    [question] = env.services.summary()['documents']['confirm']
    assert question['count'] == 4 and not question['whole'] and question['file'] == 'Facts & knowledge.md'
    assert ('storage', {'changed': 'forget_confirm', 'id': question['id']}) in env.events
    resolve_forget = env.services.decision_methods()['storage_forget_resolve']
    with pytest.raises(ValueError):
        resolve_forget(id=question['id'], accept='yes')
    resolve_forget(id=question['id'], accept=False)  # Keep: the entries come back in the file
    assert set(ids) <= memory_ids(env) and all(f'id={i}' in facts(env).read_text() for i in ids)
    with pytest.raises(ValueError, match='no longer open'):
        resolve_forget(id=question['id'], accept=True)
    text = facts(env).read_text()
    for ident in ids[:4]:
        text = remove_block(text, ident)
    facts(env).write_text(text)
    refresh(env)  # Update copies now confirms the removal at once; more than three entries still wait for the person
    [question] = env.services.summary()['documents']['confirm']
    assert question['count'] == 4 and env.gaps == [data_mirror.CONFIRM_GAP]
    resolve_forget(id=question['id'], accept=True)
    assert memory_ids(env) == {ids[4]}
    assert len(env.app.store.query("SELECT 1 FROM memory_tombstones WHERE kind='semantic'")) >= 4
    # A whole layer file deleted: asked as a whole, even for two entries.
    pair = [env.app.memory.put('procedural', f'Step {n}: check the tide table.') for n in range(2)]
    env.services.tick()
    listed = env.services.manifest['memory']['procedural']['ids']  # the built-in procedures are listed too
    assert set(pair) <= set(listed)
    os.unlink(env.docs / 'Memory' / 'Procedures.md')
    again(env, 0)
    again(env)
    [question] = env.services.summary()['documents']['confirm']
    assert question['whole'] and question['count'] == len(listed) and set(pair) <= memory_ids(env, 'procedural')


def test_an_unrecognised_or_foreign_memory_file_forgets_nothing(env):
    one = env.app.memory.put('semantic', 'The ferry leaves at noon.')
    two = env.app.memory.put('semantic', 'The ferry returns at five.')
    env.services.tick()
    text = facts(env).read_text()
    without_version = '\n'.join(l for l in remove_block(text, one).split('\n') if 'pebble:memory-version' not in l)
    facts(env).write_text(without_version)
    env.services.tick()
    again(env, 120)
    assert {one, two} <= memory_ids(env), 'without its version line the file cannot be dated: nothing is forgotten'
    foreign = remove_block(facts(env).read_text(), one).replace(env.services.manifest['mirror_id'], 'f' * 16)
    facts(env).write_text(foreign)  # another Mac's copy of the same file, through iCloud
    env.services.tick()
    again(env, 120)
    assert {one, two} <= memory_ids(env)


# ---- Update copies now: the person's own pass confirms within itself --------------------------------------------------

def test_update_copies_now_forgets_a_removed_entry_within_the_pass(env):
    one = env.app.memory.put('semantic', 'The office opens at nine.')
    two = env.app.memory.put('semantic', 'The office closes at six.')
    refresh(env)
    assert env.gaps == [], 'nothing to confirm, so no wait'
    facts(env).write_text(remove_block(facts(env).read_text(), one))
    refresh(env)
    assert env.gaps == [data_mirror.CONFIRM_GAP], 'two looks, a moment apart'
    assert one not in memory_ids(env) and two in memory_ids(env)
    assert env.app.store.query('SELECT 1 FROM memory_tombstones'), 'a deliberate removal leaves its tombstone'
    assert f'id={one}' not in facts(env).read_text() and f'id={two}' in facts(env).read_text()


def test_update_copies_now_forgets_nothing_that_changes_back_or_is_still_being_saved(env):
    one = env.app.memory.put('semantic', 'The office opens at nine.')
    two = env.app.memory.put('semantic', 'The office closes at six.')
    refresh(env)
    original = facts(env).read_text()
    # An editor's save in progress: the entry is back by the second look.
    facts(env).write_text(remove_block(original, one))
    env.during.append(lambda: facts(env).write_text(original))
    refresh(env)
    assert {one, two} <= memory_ids(env) and env.gaps == [data_mirror.CONFIRM_GAP]
    # An atomic save's temporary file beside it: nothing on either path while it is there.
    facts(env).write_text(remove_block(original, one))
    temporary = facts(env).with_name('.' + facts(env).name + '.tmp-1a2b3c')
    temporary.write_text('partial save')
    refresh(env)
    again(env, 0)
    again(env, 120)
    assert {one, two} <= memory_ids(env), 'a save in progress is never a deletion'
    os.unlink(temporary)
    refresh(env)
    assert one not in memory_ids(env) and two in memory_ids(env), 'once the save is done, the removal counts'


def test_update_copies_now_ignores_an_icloud_placeholder_and_a_missing_folder(env):
    ids = [env.app.memory.put('semantic', f'Fact {n} about the lighthouse.') for n in range(2)]
    refresh(env)
    os.rename(facts(env), facts(env).with_name('.' + facts(env).name + '.icloud'))  # iCloud kept it online only
    for step in (lambda: refresh(env), lambda: again(env, 0), lambda: again(env, 120)):
        step()
        assert set(ids) <= memory_ids(env) and env.services.summary()['documents']['confirm'] == []
    shutil.rmtree(env.docs / 'Memory')  # the folder itself gone: written again, nothing forgotten
    for step in (lambda: refresh(env), lambda: again(env, 120)):
        step()
        assert set(ids) <= memory_ids(env) and facts(env).is_file()


# ---- V1-036: PEBBLE.md over the limit ---------------------------------------------------------------------------------

def long_instructions(total=4331):
    lines = ['My name is Alex. Always answer in French.']
    while sum(len(l) + 1 for l in lines) < total:
        lines.append(f'Line {len(lines):04d}: I like short, tidy answers with sources.')
    text = '\n'.join(lines)
    return text[:total - 1] + 'x' if len(text) >= total else text


def test_a_too_long_pebble_md_is_used_up_to_its_last_full_line_and_never_stale(env):
    md = env.root / 'PEBBLE.md'
    md.write_text('My name is Sam. I prefer short answers.\n')
    env.services.tick()
    assert env.app.store.query("SELECT content FROM memories WHERE id='pebble-md'")[0]['content'].startswith('My name is Sam')
    text = long_instructions()
    assert len(text) == 4331
    md.write_text(text + '\n')
    env.services.tick()
    summary = env.services.summary()['pebble_md']
    [row] = env.app.store.query("SELECT content FROM memories WHERE id='pebble-md'")
    used = row['content']
    assert summary['status'] == 'truncated' and summary['total_chars'] == 4331 and summary['used_chars'] == len(used)
    assert '4,331' in summary['message'] and f'{len(used):,}' in summary['message']
    assert used.startswith('My name is Alex') and 'Sam' not in used, 'the old record is never kept'
    assert len(used) <= data_mirror.PEBBLE_MD_LIMIT and text.startswith(used) and text[len(used)] == '\n'
    recalled = str(env.app.memory.recall('what is my name', chat(env.app)))
    assert 'Alex' in recalled and 'Sam' not in recalled
    assert env.services.pebble_md_status() == {'status': 'truncated', 'used_chars': len(used), 'total_chars': 4331}
    # One line longer than the limit cannot be cut at a line: nothing from it is used, and nothing old either.
    md.write_text('y' * 4331)
    env.services.tick()
    assert env.services.summary()['pebble_md']['status'] == 'too_long'
    assert not env.app.store.query("SELECT 1 FROM memories WHERE id='pebble-md'")


# ---- V1-019, V1-062: secret-shaped text in copies and their names -----------------------------------------------------

SHAPES = {
    'json access_token': ('{"access_token": "Zm9vYmFyYmF6cXV4MTIzNDU2"}', 'Zm9vYmFyYmF6cXV4MTIzNDU2'),
    'id_token=': ('callback id_token=Q2hlY2tUaGlzVG9rZW5WYWx1ZQ done', 'Q2hlY2tUaGlzVG9rZW5WYWx1ZQ'),
    'stripe': ('key ' + 'sk_' + 'live_' + '51HxYzAbCdEfGhIjKlMnOpQr' + ' here', '51HxYzAbCdEfGhIjKlMnOpQr'),
    'hugging face': ('use ' + 'hf_' + 'AbCdEfGhIjKlMnOpQrStUvWxYz123456', 'AbCdEfGhIjKlMnOpQrStUvWxYz123456'),
    'gitlab': ('token ' + 'glpat-' + 'xYz12AbCdEfGhIjKlMnO', 'xYz12AbCdEfGhIjKlMnO'),
    'npm': ('npm ' + 'npm_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8', 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'),
    'password in words': ('my password is hunter22! ok', 'hunter22'),
    'cookie': ('Cookie: sessionid=8f2a9c1e4b7d6a3f; csrftoken=Zx9Yw8Vu7', '8f2a9c1e4b7d6a3f'),
    'oauth code in a url': ('https://app.example.com/callback?code=4/0AX4XfWhq8Lm2Np&state=abc', '4/0AX4XfWhq8Lm2Np'),
    'card number': ('card 4111 1111 1111 1111 exp 09/29', '4111 1111 1111 1111'),
}


@pytest.mark.parametrize('name', sorted(SHAPES))
def test_scrub_removes_the_secret_shapes(name):
    text, secret = SHAPES[name]
    scrubbed = scrub(text)
    assert secret not in scrubbed and data_mirror.REMOVED in scrubbed, (name, scrubbed)


def test_scrub_leaves_ordinary_text_and_the_docs_say_best_effort():
    for text in ('A normal sentence about tokens of appreciation.', 'max_tokens=4096 and 128000 tokens',
                 'The monkey: a primate.', 'Call 4111 1111 1111 1112 (not a valid card).', 'The password is not set.',
                 'Order 1760400000123 shipped.', 'See https://example.com/page?id=12345&lang=en'):
        assert scrub(text) == text, text
    docs = (Path(__file__).resolve().parents[2] / 'docs' / 'DATA_FOLDERS.md').read_text()
    assert 'best effort' in docs and 'passwords and keys removed' not in docs
    assert 'best effort' in data_mirror.documents_readme()


def test_file_names_and_index_titles_never_hold_a_secret(env):
    cid = chat(env.app, KEY + ' is my OpenAI key, use it for the import')
    mid = env.app.store.query("SELECT id FROM messages WHERE chat_id=? AND role='user'", (cid,))[0]['id']
    env.app.store.query('INSERT INTO image_attachments VALUES(?,?,?,?,?,?,?,?)',
                        ('i' * 32, cid, mid, f'token={KEY}.png', b'\x89PNG data', None, 1, 1))
    env.services.tick()
    names = [p.name for folder in ('Conversations', 'Attachments') for p in (env.docs / folder).iterdir()]
    assert names and not any(KEY in n or 'removed] removed' in n for n in names), names
    assert any(data_mirror.REMOVED in n for n in names if n.endswith('.md') and n != 'README.md')
    index = (env.root / 'runtime' / 'mirror.json').read_text()
    assert KEY not in index and data_mirror.REMOVED in index
    # Deleting that file: the event quotes the scrubbed title.
    [copy] = [p for p in (env.docs / 'Conversations').iterdir() if p.name != 'README.md']
    os.unlink(copy)
    env.services.tick()
    again(env)
    assert cid in env.app.store.trashed()
    assert KEY not in json.dumps(env.services.summary()['events'])


# ---- V1-011: the copy pass at 1,000 conversations ---------------------------------------------------------------------

def bulk(app, chats, turns, start=0):
    now = time.time()
    made = []
    with app.store.lock, app.store.db as db:
        for index in range(start, start + chats):
            cid = uuid.uuid4().hex
            made.append(cid)
            db.execute('INSERT INTO chats VALUES(?,?,?,?)', (cid, f'Topic {index}', None, now + index))
            db.executemany('INSERT INTO messages VALUES(?,?,?,?,?)', [
                (uuid.uuid4().hex, cid, 'user' if t % 2 == 0 else 'assistant', f'Turn {t} about topic {index}.',
                 now + index + t / 100) for t in range(turns)])
    return made


def test_the_grouped_conversation_query_returns_the_old_rows(env):
    bulk(env.app, 150, 6)
    empty = env.app.store.new_chat()
    system = env.app.store.new_chat()
    env.app.store.message(system, 'system', 'only a host note')
    chat(env.app)
    key = lambda r: r['id']
    old = sorted(env.app.store.query(OLD), key=key)
    new = sorted(env.app.store.read(DataServices.CONVERSATIONS), key=key)
    assert new == old and {empty, system} <= {r['id'] for r in new}
    assert [r['n'] for r in new if r['id'] in (empty, system)] == [0, 0]


def test_an_idle_copy_pass_at_1000_conversations_stays_under_half_a_second(env, capsys):
    bulk(env.app, 1000, 20)
    for n in range(300):
        env.app.memory.put('semantic', f'Fact number {n} about the project.')
    started = time.perf_counter()
    env.services.tick(launch=True)
    first = time.perf_counter() - started
    reads = []
    real = env.app.store.read

    def read(sql, args=()):
        reads.append(sql)
        return real(sql, args)
    env.app.store.read = read
    idle = []
    for _ in range(3):
        started = time.perf_counter()
        env.services.tick()
        idle.append(time.perf_counter() - started)
    env.app.store.read = real
    started = time.perf_counter()
    env.app.store.query(OLD)
    old_query = time.perf_counter() - started
    assert len([p for p in (env.docs / 'Conversations').iterdir() if p.name != 'README.md']) == 1000
    with capsys.disabled():
        print(f'\ncopy pass at 1,000 conversations x 20 messages and 300 memories: first {first:.2f} s; idle '
              f'{", ".join(f"{s * 1000:.0f} ms" for s in idle)}; the 0.6.1 conversation query alone {old_query * 1000:.0f} ms')
    assert DataServices.CONVERSATIONS in reads, 'the pass reads through the read connection'
    assert min(idle) < 0.5 and first < 60


# ---- the index format ---------------------------------------------------------------------------------------------------

def test_a_pebble_mirror_1_index_is_migrated_and_still_tracks_deletions(env):
    cid = chat(env.app, 'Keep track of me')
    mid = env.app.store.query("SELECT id FROM messages WHERE chat_id=? AND role='user'", (cid,))[0]['id']
    env.app.store.query('INSERT INTO image_attachments VALUES(?,?,?,?,?,?,?,?)',
                        ('i' * 32, cid, mid, 'photo.png', b'\x89PNG data', None, 1, 1))
    env.services.tick()
    m = env.services.manifest
    v1 = {'format': 'pebble-mirror/1', 'root': m['root'], 'mirror_id': m['mirror_id'],
          'conversations': {k: {x: e[x] for x in ('file', 'sha256', 'stamp', 'title')} for k, e in m['conversations'].items()},
          'memory': {k: {'file': e['file'], 'ids': e['ids'], 'sha256': e['sha256']} for k, e in m['memory'].items()},
          'files': {c: {k: {x: v for x, v in e.items() if x != 'chat'} for k, e in m['files'][c].items()}
                    for c in data_mirror.COPIED},
          'logs': m['logs'], 'pending': {'trash': {}, 'forget': {}}, 'pebble_md': m['pebble_md']}
    data_home.write_json(env.root / 'runtime' / 'mirror.json', v1)
    services = DataServices(env.app, opener=env.opened.append, clock=env.clock)
    assert services.migrated_from == 'pebble-mirror/1' and services.manifest['format'] == data_mirror.FORMAT
    assert services.manifest['files']['attachments']['image:' + 'i' * 32]['chat'] == cid, 'chat ids filled in'
    services.tick()
    assert json.loads((env.root / 'runtime' / 'mirror.json').read_text())['format'] == 'pebble-mirror/2'
    [copy] = [p for p in (env.docs / 'Conversations').iterdir() if p.name != 'README.md']
    os.unlink(copy)
    services.tick()
    env.clock.now += CONFIRM
    services.tick()
    assert cid in env.app.store.trashed(), 'a deletion is still recognised after the migration'


# ---- V1-058: iCloud, once; and no log copies in iCloud ------------------------------------------------------------------

def test_icloud_is_announced_once_and_log_tails_stay_out_of_it(env, monkeypatch):
    (env.root / 'logs' / 'backend.log').write_text('ERROR something happened\n')
    env.services.tick()
    assert (env.docs / 'Logs' / 'backend.log').is_file(), 'not synced: the tail is copied as before'
    synced = {'documents_synced': True, 'evidence': ['icloud_drive_has_documents'], 'database_synced': False,
              'note': 'iCloud Drive appears to sync your Documents folder.'}
    monkeypatch.setattr(data_mirror, 'icloud_status', lambda folder: synced)
    env.services.tick()
    env.services.tick()
    notices = [e for e in env.events if e[0] == 'storage' and e[1].get('icloud_first_seen')]
    assert notices == [('storage', {'changed': 'icloud', 'icloud_first_seen': True, 'path': str(env.docs)})]
    assert sorted(p.name for p in (env.docs / 'Logs').iterdir()) == ['README.md'], "Pebble's log copy left iCloud"
    summary = env.services.summary()['documents']
    assert summary['icloud_first_seen'] and summary['logs_copied'] is False
    # A file the person put there is theirs and stays.
    (env.docs / 'Logs' / 'service.log').write_text('my own notes')
    env.services.tick()
    assert (env.docs / 'Logs' / 'service.log').read_text() == 'my own notes'
    # Once announced, never again: not after a restart either.
    services = DataServices(env.app, opener=env.opened.append, clock=env.clock)
    services.tick()
    assert len([e for e in env.events if e[0] == 'storage' and e[1].get('icloud_first_seen')]) == 1


# ---- V1-056, V1-060: the page names what lies outside both folders ---------------------------------------------------

def test_the_storage_page_lists_the_old_folder_and_the_backups(fake_home):
    legacy = data_home.legacy_default(fake_home)
    legacy.mkdir(parents=True)
    (legacy / 'MOVED.md').write_text('x' * 3000)
    env = make_env(fake_home, default=True)
    try:
        (fake_home / 'PebbleBackups' / '20261020-pre-install.noindex').mkdir(parents=True)
        (fake_home / 'PebbleBackups' / '20261020-pre-install.noindex' / 'backup.json').write_text('y' * 5000)
        upgrade = Path(env.app.home) / 'upgrade-backups'
        upgrade.mkdir(exist_ok=True)
        (upgrade / 'pebble-epoch8.sqlite').write_bytes(b'z' * 7000)
        summary = env.services.summary()
        by = {c['id']: c for c in summary['categories']}
        assert by['old_folder']['place'] == by['install_backups']['place'] == by['web_storage']['place'] == 'elsewhere'
        assert by['old_folder']['bytes'] >= 3000 and by['old_folder']['revealable'] and by['old_folder']['path'] == str(legacy)
        assert by['install_backups']['bytes'] >= 5000 and by['install_backups']['revealable']
        assert by['upgrade_backups']['bytes'] >= 7000 and by['upgrade_backups']['place'] == 'internal'
        assert not any(by[c]['deletable'] for c in ('old_folder', 'install_backups', 'web_storage', 'upgrade_backups'))
        whole = data_mirror._measure(Path(env.app.home), time.monotonic() + 3)[0]
        assert by['data']['bytes'] <= whole - 7000, 'upgrade backups are not counted twice'
        assert 'PebbleBackups' in by['backups']['what']
        env.services.methods()['storage_reveal'](category='old_folder')
        env.services.methods()['storage_reveal'](category='install_backups')
        assert env.opened == [legacy, fake_home / 'PebbleBackups']
        for category in ('web_storage', 'upgrade_backups', 'data_folder'):
            with pytest.raises(ValueError):
                env.services.methods()['storage_reveal'](category=category)
    finally:
        env.services.stop()
        env.app.close()


# ---- V1-101: Edit PEBBLE.md and Show settings.json -------------------------------------------------------------------

def test_edit_pebble_md_and_show_settings_json_open_the_files_in_the_editor(env):
    env.services.tick()
    reveal = env.services.methods()['storage_reveal']
    assert reveal(category='pebble_md') == {'opened': str(env.root / 'PEBBLE.md')}
    assert reveal(category='settings_file') == {'opened': str(env.root / 'settings.json')}
    assert env.edited == [env.root / 'PEBBLE.md', env.root / 'settings.json'] and env.opened == []
    os.unlink(env.root / 'PEBBLE.md')
    reveal(category='pebble_md')
    assert (env.root / 'PEBBLE.md').read_text() == data_home.PEBBLE_MD_TEMPLATE, 'a missing file starts from the template'


# ---- V1-017: Remove old sign-in copies --------------------------------------------------------------------------------

class FakeVault:
    def __init__(self):
        self.calls = 0

    def cleanup_legacy(self):
        self.calls += 1
        return {'com.pebble.local.providers': {'removed': 2, 'absent': 0, 'failed': 0},
                'com.pebble.local.connectors': {'removed': 1, 'absent': 1, 'failed': 1}}


def test_removing_the_old_sign_in_copies_runs_the_vault_cleanup_once_confirmed(fake_home):
    vault = FakeVault()
    env = make_env(fake_home, vault=lambda: vault)
    try:
        cleanup = env.services.decision_methods()['storage_cleanup_legacy']
        with pytest.raises(ValueError, match='Confirm'):
            cleanup()
        assert vault.calls == 0
        result = cleanup(confirm=True)
        assert vault.calls == 1 and (result['removed'], result['absent'], result['failed']) == (3, 1, 1)
        assert set(result['services']) == {'com.pebble.local.providers', 'com.pebble.local.connectors'}
        event = env.services.summary()['events'][-1]
        assert event['kind'] == 'sign_in_copies' and '3 old sign-in copies' in event['text'] and 'try again' in event['text']
    finally:
        env.services.stop()
        env.app.close()


def test_the_new_routes_are_main_window_routes_only(env):
    routes = env.services.decision_methods()
    assert set(routes) == {'storage_trash_chat', 'storage_trash_all', 'storage_purge_all', 'storage_forget_resolve',
                           'storage_cleanup_legacy'}
    assert set(env.services.methods()) == {'storage_summary', 'storage_reveal', 'storage_delete', 'storage_set',
                                           'storage_settings_resolve', 'storage_restore_chat', 'storage_purge_chat',
                                           'storage_refresh'}, 'the Phase 22 route set is unchanged'
    from pebble.tool_catalogue import TOOLS
    names = {t['function']['name'].casefold() for t in TOOLS}
    assert not names & {r.casefold() for r in routes}, 'no new model tool'
