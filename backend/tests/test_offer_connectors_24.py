"""Phase 24 (parent): Phase 24 trial 1's campaign failed validate_phase16's
untrusted_mail_output_cannot_add_web_research_app_or_screen_tools. V1-032's large_offer gave every no-project chat
WebSearch, ReadURL and Weather above 8K, so a mail-reply run that reads untrusted connector output also held fetch tools,
the exfiltration pairing Phase 16 rules out. A run that offers connector tools now gets the web only for the person's
own current-fact wording; a plain chat keeps V1-032's offer.
"""
from pebble import runner
from pebble.app import App

LARGE = 32768
MAIL = 'Reply to the newest message in my Pebble test inbox to say the fixture works.'  # validate_phase16's c4 request
FACT = 'What is the latest exchange rate for the euro today?'


def offer(app, cid, text, *, web=True, connectors):
    return runner.large_offer(app, cid, None, text, [], None, LARGE, {'web': web}, connectors=connectors)


def test_a_connector_run_gets_the_web_only_for_the_persons_current_fact_wording(tmp_path):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        cid = app.new_chat()['id']
        assert not runner.WEB_TOOLS & offer(app, cid, MAIL, connectors=True), 'no fetch tools beside untrusted mail output'
        assert runner.WEB_TOOLS <= offer(app, cid, MAIL, connectors=False), 'V1-032 still offers the web in a plain chat'
        assert runner.WEB_TOOLS <= offer(app, cid, FACT, connectors=True), 'the person asked for a current fact'
        assert not runner.WEB_TOOLS & offer(app, cid, FACT, web=False, connectors=True), 'the permission still decides'
    finally:
        app.close()
