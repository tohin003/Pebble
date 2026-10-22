"""Native clarification wakes the parent before the child finishes its work."""

import json
import threading

from test_agent_manager import (
    ANSWER, QUESTIONS, call, env, eventually, final, node, parent_question,
    root, root_tool, settled, spawn,
)


def test_native_answer_wakes_root_while_child_still_running(env):
    cid, rid = root(env)
    continuing, finish = threading.Event(), threading.Event()

    def script(_provider, step, _messages, _tools, cancel):
        if step == 1:
            return call('AskParentQuestion', {'questions': json.dumps(QUESTIONS)}, 'clarify')
        continuing.set()
        while not finish.wait(.01):
            if cancel.is_set():
                raise InterruptedError('Fixture child stopped.')
        return final('Scoped work finished after clarification.')

    child = spawn(env, cid, rid, 'native-wake', script, tools=('AskParentQuestion',))
    question = parent_question(env, cid, rid, child)
    promoted = root_tool(env, cid, rid, 'PromoteAgentQuestion', {'question_id': question['id']})
    wait = root_tool(env, cid, rid, 'WaitForAgents', {'agent_ids': json.dumps([child['id']])}, 'wait-native-wake')
    assert wait['status'] == 'awaiting_agent_results'
    env.app.runs.transition(rid, 'waiting_input')
    env.app.interactions.answer(cid, promoted['question_id'], 1, ANSWER)
    assert continuing.wait(3)
    assert node(env, cid, rid, child)['status'] == 'running'
    saved = env.app.store.query('SELECT status,result FROM agent_waits WHERE run_id=? AND call_id=?', (rid, 'wait-native-wake'))[0]
    assert saved['status'] == 'ready'
    ready = json.loads(saved['result'])
    assert ready['reason'] == 'human_followup' and not ready['agent_ids']
    assert 'answer-' + promoted['question_id'] in ready['input_ids']
    inputs = env.app.store.query('SELECT kind,origin FROM run_inbox WHERE run_id=? ORDER BY sequence', (rid,))
    assert {'kind': 'question_answer', 'origin': 'human'} in inputs
    assert {'kind': 'agent_wait_ready', 'origin': 'agent'} in inputs
    assert env.app.store.query('SELECT state FROM run_continuations WHERE run_id=?', (rid,))[0]['state'] == 'queued'
    finish.set()
    settled(env, cid, rid, child)
