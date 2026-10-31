"""Phase 46 (1.9.28, the lead's merge of w8-readonly's follow-up): the digits of a folder in an absolute path named by
the FINAL line are no value of the answer (found when the tests ran under /private/tmp/claude-501/...: "the value 501
does not appear in any code file this run read"). The project sits under a short base, since _grounding reads path
tokens of at most about 200 characters."""

import shutil
import tempfile
from pathlib import Path

from test_security_review_1928 import BASE, RN, grounded, project


def test_digits_in_the_folders_of_an_absolute_path_are_not_the_answers_values():
    short = Path(tempfile.mkdtemp(prefix='pg', dir='/private/tmp'))
    try:
        root = project(short / 'proj-2024' / 'build-501', BASE)
        reads = [str(root.resolve() / rel) for rel in BASE]
        assert grounded(root, BASE, f'{root.resolve()}/netkit/client.py:_give_up 5', RN, reads) == []
        [(issue, _)] = grounded(root, BASE, f'{root.resolve()}/netkit/client.py:_give_up 3', RN, reads)
        assert 'this run read it in netkit/pool.py' in issue and '2024' not in issue and '501' not in issue
    finally:
        shutil.rmtree(short, ignore_errors=True)
