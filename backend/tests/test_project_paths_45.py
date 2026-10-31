"""Phase 45 (1.9.4, F067): a project file named by its absolute path inside the selected project is the same file
(the audit's real task was refused with "Paths must stay inside the selected project" for a file that was); anything
outside the project, or through a symlink, is still refused."""
import os
from pathlib import Path

import pytest

from pebble.policy import PolicyError, Workspace


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'proj'
    (root / 'src').mkdir(parents=True)
    (root / 'src' / 'a.py').write_text('x = 1\n')
    return root


def test_an_absolute_path_inside_the_project_is_its_relative_file(project):
    ws = Workspace(str(project))
    real = Path(os.path.realpath(project))
    assert ws.path(str(real / 'src' / 'a.py')) == ws.path('src/a.py')
    assert ws.path(str(real / 'notes.txt'), create=True) == ws.path('notes.txt', create=True)


def test_outside_the_project_or_through_a_link_is_still_refused(project, tmp_path):
    ws = Workspace(str(project))
    real = Path(os.path.realpath(project))
    (tmp_path / 'outside.txt').write_text('no')
    for bad in (str(Path(os.path.realpath(tmp_path)) / 'outside.txt'), str(real) + '-sibling/x.txt',
                str(real / '..' / 'outside.txt'), '/etc/hosts'):
        with pytest.raises(PolicyError):
            ws.path(bad)
    (project / 'link').symlink_to(tmp_path)
    with pytest.raises(PolicyError):
        ws.path(str(real / 'link' / 'outside.txt'))
