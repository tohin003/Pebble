"""Phase 46: an earlier message's output contract is checked again in a later turn that keeps it.

Measured first (offline, requirements.extract on the private golden-extra case d-lh-91, 1.9.26): turn 1's "The report
printed by `python3 -m inventory.cli data/stock.json` must stay byte-identical to samples/report.txt" gave no
requirement (an unrecognised form), and the ledger was built from the current message only, so turns 2-5 never checked
the report and turn 6's "must still match samples/report.txt byte for byte" checked only the suite. On the golden
held-out long_horizon family ("an early constraint matters at the end") 1.9.26 had 5 false completions on sol and 3 on
Opus.
"""

import json

import pytest

from pebble import requirements, runner
from test_context_cost_24 import scripted_app

REPORT = 'python3 -m inventory.cli data/stock.json'


@pytest.mark.parametrize('sentence', [
    f'The report printed by `{REPORT}` must stay byte-identical to samples/report.txt.',
    f'The output of `{REPORT}` must still match samples/report.txt byte for byte.',
    f'The output of `{REPORT}` must stay byte-identical to samples/report.txt.'])
def test_every_everyday_form_of_the_output_contract_is_checked(sentence):
    assert [(r['kind'], r['command'], r['path']) for r in requirements.extract(sentence)] == [
        ('output_equals', REPORT, 'samples/report.txt')]


def test_a_carried_contract_is_named_as_an_earlier_messages_in_the_note_and_the_gate():
    # One design since the integration with the second opinion (2026-10-28): requirements.carried takes the earlier
    # runs' ledgers and carries a contract into a turn that names its file or keeps the output as it is.
    earlier = [requirements.extract(f'Add low_stock(). The report printed by `{REPORT}` must stay byte-identical to '
                                    'samples/report.txt.')]
    kept = requirements.carried(earlier, 'Before I merge: the full text report must still match samples/report.txt '
                                         'byte for byte.')
    assert [(r['command'], r['path'], r['carried']) for r in kept] == [(REPORT, 'samples/report.txt', True)]
    assert 'an earlier message of this conversation asked this' in requirements.checks_note(kept)
    assert requirements._said(kept[0]) == 'An earlier message of this conversation says'
    assert requirements._said(earlier[0][0]) == 'The request says'


def test_a_follow_up_run_records_the_earlier_contract_in_its_ledger(tmp_path):
    project = tmp_path / 'inventory'
    (project / 'samples').mkdir(parents=True)
    (project / 'samples' / 'report.txt').write_text('SKU QTY\n')
    app = scripted_app(tmp_path, 32768)

    def complete(messages, tools, cancel, token, deliberate=False):
        token('Done.')
        return {'role': 'assistant', 'content': 'Done.'}
    app.model.complete = complete
    try:
        chat = app.new_chat(str(project))
        ledgers = []
        for text in (f'Add low_stock(). The report printed by `{REPORT}` must stay byte-identical to samples/report.txt.',
                     'Now rename Store.remove to Store.take.',
                     'Switch prices to integer cents; the report must still match samples/report.txt byte for byte.'):
            app.store.message(chat['id'], 'user', text)  # as App.send stores the person's message before its run
            runner.run(app, app.chat(chat['id']), text, False, [])
            latest = app.runs.list(chat['id'])[0]['id']
            noted = app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='requirements'", (latest,))
            ledgers.append(json.loads(noted[0]['payload'])['requirements'] if noted else [])
        assert ledgers[1] == []  # a turn that says nothing about the output may change it on purpose
        assert [(r['kind'], r['path'], r.get('carried')) for r in ledgers[2] if r['kind'] == 'output_equals'] == [
            ('output_equals', 'samples/report.txt', True)]
    finally:
        app.active = app.active_run = None
        app.close()
