"""Phase 45 (1.9.11, F194 from audit round R6): an approval refused before anything ran (in R6 a mouse click whose
window had changed since the card; here an edit whose file changed since review) used to leave its run waiting for a
review that no longer existed: no reply, and the companion kept saying "Waiting for your review". The run now continues
with the refusal, as after a Deny, and the model answers."""
import json

import pytest

from pebble.policy import PolicyError
from test_approval_handoff_22 import (NOTES, call, env, final, no_network, pending, replies, settled)  # noqa: F401


def test_a_refused_approval_lets_its_run_answer(env):
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    (env.project / 'NOTES.md').write_text('# Notes\n')
    env.script = [call('Read', {'file_path': 'NOTES.md'}, 'read'),
                  call('Write', {'file_path': 'NOTES.md', 'content': NOTES}, 'write'),
                  final('The change awaits your approval.\nFINAL: staged'),
                  call('Read', {'file_path': 'NOTES.md'}, 'reread'),  # the host's audit: the file it read changed
                  final('NOTES.md changed before the edit could apply; nothing was written.\nFINAL: refused')]
    app.work(chat, 'Add a line about add to NOTES.md. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 3)
    (env.project / 'NOTES.md').write_text('# Notes\n\nChanged by someone else meanwhile.\n')
    with pytest.raises(PolicyError):
        app.approve(cid, pending(app, cid, 'edit')['id'])
    settled(env, app, rid, 'completed', 5)
    assert replies(app, cid)[-1].endswith('FINAL: refused')
    resumed = json.dumps(env.requests[3])
    assert 'failed' in resumed, 'the model is told the approved step was refused'
    assert (env.project / 'NOTES.md').read_text().endswith('Changed by someone else meanwhile.\n'), 'nothing written'
