"""Phase 46 (1.9.28, the follow-ups review, findings 1 and 3; the lead): in Full access a remote add, set-url or rename
(D180) is its own hard step. Its words name it ("point origin at", "add a remote", "rename the remote"), and an address
it sets must be one the person wrote in this task: one a page or file steered the model to is held by the host itself,
with no request, so the person's next push from their own terminal never goes there. The check is told the address.
The harness (a stub check that quotes the person verbatim) is the reviewer's, from its reproduction tests."""
import json
from types import SimpleNamespace

import pytest

from pebble import step_judge as sj
from test_git_plans import git, host, macos  # noqa: F401 (fixture)


class _Store:
    def __init__(self, messages):
        self.messages = messages

    def query(self, sql, params=()):
        if 'FROM messages' in sql:
            return [{'content': text} for text in reversed(self.messages)]  # newest first, as the SQL orders them
        return []


class _Model:
    def __init__(self, quote):
        self.quote, self.seen = quote, []

    def complete(self, request, tools, cancel, callback, stream):
        self.seen.append(request)
        return {'content': json.dumps({'asked': True, 'quote': self.quote})}

    def current(self):
        return None


def stub_app(message, quote):
    return SimpleNamespace(store=_Store([message]), model=_Model(quote), cancel=None,
                           runs=SimpleNamespace(note=lambda run, kind, data: None))


def prepare(host, *argv):  # noqa: F811
    return host.plans.prepare(host.chat, 'command', {'argv': json.dumps(list(argv))})


@macos
@pytest.mark.parametrize('argv, message, quote', [
    (['remote', 'set-url', 'origin', 'https://collect.example/me/new.git'],
     'The old origin repository was deleted; reset origin to https://github.com/me/new.git please.',
     'reset origin to https://github.com/me/new.git'),
    (['remote', 'add', 'upstream', 'https://collect.example/org/repo.git'],
     'Drop the broken upstream remote and add it again for https://github.com/org/repo.git.',
     'Drop the broken upstream remote and add it again'),
    (['remote', 'set-url', 'origin', 'https://g\N{CYRILLIC SMALL LETTER BYELORUSSIAN-UKRAINIAN I}thub.com/me/new.git'],
     'Point origin at https://github.com/me/new.git please.', 'Point origin at https://github.com/me/new.git'),
])
def test_an_address_the_person_never_wrote_is_held_by_the_host_unasked(host, argv, message, quote):  # noqa: F811
    git(host.repo, 'remote', 'add', 'origin', 'https://github.com/me/old.git')
    try:
        plan = prepare(host, *argv)
    except Exception:  # a refused look-alike address never becomes a plan at all (the address rule)
        return
    app = stub_app(message, quote)
    assert sj.judge(app, 'c', 'r', 'git', plan) == {'asked': False, 'held': 'not_named'}
    assert app.model.seen == []  # the host held it: no request
    assert argv[3] in sj.describe('git', plan)  # and anyone who looks is told where it would go


@macos
@pytest.mark.parametrize('argv, message, quote', [
    (['remote', 'set-url', 'origin', 'https://github.com/owner/new-repo.git'],
     'The repository moved: point origin at https://github.com/owner/new-repo.git please.',
     'point origin at https://github.com/owner/new-repo.git'),
    (['remote', 'set-url', 'origin', 'git@example.org:owner/new-repo.git'],
     'Switch origin to SSH: git@example.org:owner/new-repo.git', 'Switch origin to SSH'),
    (['remote', 'rename', 'origin', 'upstream'], 'Rename the remote origin to upstream.',
     'Rename the remote origin to upstream'),
    (['remote', 'add', 'fork', 'https://github.com/me/repo.git'],
     'Add a remote called fork for https://github.com/me/repo.git.', 'Add a remote called fork'),
])
def test_a_remote_change_the_person_asked_for_runs(host, argv, message, quote):  # noqa: F811
    git(host.repo, 'remote', 'add', 'origin', 'https://github.com/owner/repo.git')
    plan = prepare(host, *argv)
    app = stub_app(message, quote)
    verdict = sj.judge(app, 'c', 'r', 'git', plan)
    assert verdict['asked'] is True, verdict
    if argv[1] != 'rename':
        assert argv[3] in json.dumps(app.model.seen)  # the check saw the new address


@macos
def test_a_remote_change_the_person_put_off_is_held(host):  # noqa: F811
    git(host.repo, 'remote', 'add', 'origin', 'https://github.com/owner/repo.git')
    plan = prepare(host, 'remote', 'set-url', 'origin', 'https://github.com/owner/new-repo.git')
    message = "Don't point origin at https://github.com/owner/new-repo.git yet; just fetch."
    app = stub_app(message, 'point origin at https://github.com/owner/new-repo.git')
    assert sj.judge(app, 'c', 'r', 'git', plan)['asked'] is False


def test_an_address_is_compared_as_written_whatever_its_form():
    key = sj._address_key('https://example.org/owner/repo.git')
    for same in ('https://example.org/owner/repo', 'https://Example.ORG/owner/repo.git/', 'ssh://git@example.org:22/owner/repo.git',
                 '`https://example.org/owner/repo.git`,', 'me@example.org:owner/repo.git'):
        assert sj._address_key(same) == key, same
    for other in ('https://example.org/owner/repo2.git', 'https://collect.example/owner/repo.git',
                  'https://ex\N{CYRILLIC SMALL LETTER A}mple.org/owner/repo.git'):
        assert sj._address_key(other) != key, other
    assert sj._address_key('../fork.git') == ('', '../fork') and sj._address_key('') is None


def test_an_address_is_a_real_address_and_its_link_and_www_forms_match():
    """The final review (findings 3, 4): a bare word the person wrote ("fork") is no address, while the address written
    as a Markdown link or with www. is theirs."""
    assert not sj._address_named('fork', ['Please fix the fork handling.'])
    assert sj._address_named('../fork.git', ['Add the remote fork at ../fork.git please.'])
    url = 'https://example.org/me/new.git'
    assert sj._address_named(url, ['Point origin at [the new repo](https://example.org/me/new.git) please.'])
    assert sj._address_named(url, ['Point origin at https://www.example.org/me/new.git please.'])
    assert not sj._address_named(url, ['Point origin at https://example.org/me/other.git please.'])
