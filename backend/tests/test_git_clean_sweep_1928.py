"""Phase 46 (1.9.28, golden held-out safety_actions: one sol cell unsafe on 1.9.27; b-new G1): a git clean without named
paths deletes only generated output.

1.9.27 turned git clean on by default (D174): the plan deletes exactly what `git clean -n` listed, on one card. Offline,
a reviewed `["clean", "-fdX"]` in d-sa-01's shape with the irreplaceable exports/ git-ignored listed build/, dist/ and
exports/ on one card; the driver approved it and the exports were gone, with "FINAL: done" (private d-sa-91); a
`["clean", "-fd"]` deleted the person's untracked notes with the untracked site/ (private d-sa-93). Without named paths,
a deleting clean may now delete only what requirements.generated_files counts as generated (caches, *.egg-info, *.pyc,
and build-output folders Git ignores); otherwise it is refused, naming what it would also have deleted and the narrower
form. Named paths, dry runs, the private-file refusal and the person's git_decisions switch are unchanged.

Restated for w12-bnew's security review (w12b): each listed path is judged by its own name and the folders above it,
never by what it holds, and a build-output folder counts only where the root .gitignore names it, under -X as under -x.
"""
import json

import pytest

from pebble.policy import PolicyError
from test_git_commands_46 import command, wip
from test_git_everyday_46 import decide, prepare
from test_git_plans import git, host, macos  # noqa: F401 (fixture)


def ignore(host, text):  # noqa: F811
    (host.repo / '.gitignore').write_text(text)
    git(host.repo, 'add', '.gitignore')
    git(host.repo, 'commit', '-q', '-m', 'Ignore')


def make(host, *paths):  # noqa: F811
    for rel in paths:
        (host.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (host.repo / rel).write_text(rel + '\n')


@macos
def test_an_unnamed_clean_that_would_sweep_an_ignored_data_folder_is_refused_with_the_narrower_form(host):  # noqa: F811
    ignore(host, 'build/\ndist/\nexports/\n')
    make(host, 'build/report.html', 'dist/ledger.tar.gz', 'exports/2026-09.csv', 'exports/2026-10.csv')
    with pytest.raises(PolicyError) as refused:
        prepare(host, 'clean', '-fdX')
    text = str(refused.value)
    assert text.startswith('git clean without named paths would also delete exports/, which is not build output')
    assert 'nothing was removed' in text
    assert json.dumps(['clean', '-f', '-d', '-X', '--', 'build/', 'dist/']) in text
    assert (host.repo / 'exports' / '2026-09.csv').exists() and (host.repo / 'build' / 'report.html').exists()
    plan, cleaned = command(host, 'clean', '-f', '-d', '-X', '--', 'build/', 'dist/')  # the narrower form runs
    assert cleaned['state'] == 'applied' and plan['bindings']['clean']['paths'] == ['build/', 'dist/']
    assert not (host.repo / 'build').exists() and not (host.repo / 'dist').exists()
    assert (host.repo / 'exports' / '2026-10.csv').read_text() == 'exports/2026-10.csv\n'


@macos
def test_untracked_work_beside_build_output_is_named_and_kept(host):  # noqa: F811
    wip(host)
    make(host, 'site/index.html', 'notes/launch-plan.md')
    with pytest.raises(PolicyError) as refused:
        prepare(host, 'clean', '-fd')
    text = str(refused.value)
    assert 'notes/, scratch.md, site/, which are not build output or a cache' in text
    assert json.dumps(['clean', '-f', '-d', '--', '<path>']) in text
    listed = prepare(host, 'clean', '-n', '-d')  # a dry run lists anything, as before
    assert sorted(listed['bindings']['clean']['paths']) == ['notes/', 'scratch.md', 'site/']
    _, cleaned = command(host, 'clean', '-f', '-d', '--', 'site')  # named paths are unchanged
    assert cleaned['state'] == 'applied' and not (host.repo / 'site').exists()
    assert (host.repo / 'notes' / 'launch-plan.md').exists() and (host.repo / 'scratch.md').read_text() == 'ideas\n'


@macos
def test_caches_and_ignored_build_output_still_go_without_names(host):  # noqa: F811
    ignore(host, 'build/\n*.pyc\n__pycache__/\n*.egg-info/\nlocal/\n')
    make(host, 'pkg/__init__.py')
    git(host.repo, 'add', 'pkg/__init__.py')
    git(host.repo, 'commit', '-q', '-m', 'Package')
    make(host, 'build/lib/a.py', 'pkg/__pycache__/a.cpython-312.pyc', 'pkg/b.pyc', 'pkg.egg-info/PKG-INFO')
    plan, cleaned = command(host, 'clean', '-f', '-d', '-X')  # a tracked package: Git lists its caches one by one
    assert cleaned['state'] == 'applied'
    assert sorted(plan['bindings']['clean']['paths']) == ['build/', 'pkg.egg-info/', 'pkg/__pycache__/', 'pkg/b.pyc']
    assert not (host.repo / 'build').exists() and not (host.repo / 'pkg' / '__pycache__').exists()
    assert (host.repo / 'pkg' / '__init__.py').exists()
    make(host, 'local/__pycache__/c.pyc', 'local/review.db')  # a folder listed whole that also holds data
    with pytest.raises(PolicyError, match='would also delete local/, which is not build output'):
        prepare(host, 'clean', '-fdX')
    assert (host.repo / 'local' / 'review.db').exists()


@macos
def test_a_listed_folder_is_judged_by_its_own_name_never_by_what_it_holds(host):  # noqa: F811
    """w12-bnew's security review: under -X, an ignored results/ holding results/out/ and results/build/ counted as
    build output (a folder named out or build anywhere inside it) and was deleted whole without being named."""
    ignore(host, 'results/\nbuild/\n__pycache__/\n')
    make(host, 'results/out/run-2026-10.csv', 'results/build/model-final.bin', 'build/app.bin')
    for options in ('-fdX', '-fdx'):
        with pytest.raises(PolicyError) as refused:
            prepare(host, 'clean', options)
        text = str(refused.value)
        assert 'would also delete results/, which is not build output' in text and 'nothing was removed' in text
        assert json.dumps(['clean', '-f', '-d', '-' + options[-1], '--', 'build/']) in text
    assert (host.repo / 'results' / 'out' / 'run-2026-10.csv').exists()
    assert (host.repo / 'results' / 'build' / 'model-final.bin').exists()
    # restated (w12b): an untracked folder Git lists whole is not build output for holding only caches; named, it goes
    make(host, 'old/__pycache__/a.cpython-312.pyc')
    with pytest.raises(PolicyError, match='would also delete old/, results/, which are not build output'):
        prepare(host, 'clean', '-fdX')
    _, cleaned = command(host, 'clean', '-f', '-d', '-X', '--', 'old/', 'build/')
    assert cleaned['state'] == 'applied' and not (host.repo / 'old').exists() and not (host.repo / 'build').exists()
    assert (host.repo / 'results' / 'out' / 'run-2026-10.csv').exists()
    (host.repo / 'build').write_text('#!/bin/sh\necho build\n')  # an untracked file named build is no build folder
    with pytest.raises(PolicyError, match='would also delete build, results/, which are not build output'):
        prepare(host, 'clean', '-fdx')
    assert (host.repo / 'build').read_text().startswith('#!/bin/sh')


@macos
def test_an_ignored_output_folder_the_root_gitignore_does_not_name_is_not_generated_under_X(host):  # noqa: F811
    """-X proves that Git ignores a path, not that it is build output: `*.csv` ignores data/out/ too."""
    ignore(host, '*.csv\ndist/\n')
    make(host, 'data/out/2026-10.csv', 'dist/app.tar.gz')
    with pytest.raises(PolicyError) as refused:
        prepare(host, 'clean', '-fdX')
    assert 'would also delete data/, which is not build output' in str(refused.value)
    assert json.dumps(['clean', '-f', '-d', '-X', '--', 'dist/']) in str(refused.value)
    assert (host.repo / 'data' / 'out' / '2026-10.csv').exists()


@macos
def test_a_build_folder_git_does_not_ignore_is_not_generated_output_and_the_switch_still_decides(host):  # noqa: F811
    make(host, 'out/results.csv')  # an untracked out/ that no .gitignore names (generated_files' rule)
    with pytest.raises(PolicyError, match='would also delete out/'):
        prepare(host, 'clean', '-fd')
    decide(host, clean=False)  # D174: the person's switch still refuses every deleting clean first
    with pytest.raises(PolicyError, match="person's decision"):
        prepare(host, 'clean', '-fd')
