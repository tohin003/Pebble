"""Provider boundaries record honest budgets and never send host markers."""

import copy
import json

from pebble.app import App
from pebble.model import Model


def test_cloud_budget_failure_is_journaled_before_any_inference(tmp_path):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    requests = []
    try:
        app.model_save(name='Context fixture', url='http://127.0.0.1:9', model='fixture', context=4096)
        app.model.request = lambda *args, **kwargs: requests.append(args)
        chat = app.new_chat()
        goal = 'Explain ' + '漢字' * 8000
        app.work(chat, goal, False, [])
        rid = app.runs.list(chat['id'])[0]['id']
        run = app.run_get(chat['id'], rid)
        assert run['state'] == 'failed' and not requests
        manifest = run['context']['manifest']
        assert not manifest['dispatchable'] and manifest['status'] == 'exceeded'
        assert manifest['input_tokens']['protected_floor'] > manifest['limits']['input']
        assert manifest['estimation']['window_method'] == 'o200k_local_estimate'
        assert '漢字' not in json.dumps(manifest, ensure_ascii=False)
    finally:
        app.close()


def test_local_template_counter_and_wire_strip_all_host_markers():
    model = Model.__new__(Model)
    templates, manifests = [], []

    def request(path, body):
        if path == '/apply-template':
            templates.append(copy.deepcopy(body))
            return {'prompt': ''.join(m['content'] for m in body['messages'])}
        return {'tokens': [0] * len(body['content'])}

    model.json_request, model.context_observer = request, manifests.append
    body = {'max_tokens': 1800, 'messages': [
        {'role': 'system', 'content': 'policy'},
        {'role': 'user', 'content': 'goal', '_pebble_current': True},
        {'role': 'user', 'content': 'plan', '_pebble_plan': True},
        {'role': 'user', 'content': 'evidence', '_pebble_checkpoint': True},
        {'role': 'user', 'content': 'inbox', '_pebble_inbox': 'human'},
        {'role': 'user', 'content': 'skill', '_pebble_skill': True},
        {'role': 'user', 'content': 'source', '_pebble_source': True}]}
    before = copy.deepcopy(body)
    result = model.budget(body)
    assert body == before and result['max_tokens'] == 1800
    assert all(not any(k.startswith('_pebble_') for k in m) for m in result['messages'])
    assert all(not any(k.startswith('_pebble_') for k in m) for b in templates for m in b['messages'])
    manifest = manifests[0]
    assert manifest['estimation']['window_method'] == 'llama_template_tokens'
    assert manifest['limits']['output_reserve'] == result['max_tokens']
    assert manifest['input_tokens']['after'] + result['max_tokens'] <= 4000
    assert manifest['tools']['count'] == 0
