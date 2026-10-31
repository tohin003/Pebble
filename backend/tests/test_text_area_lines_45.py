"""Phase 45 (1.9.16, Phase 46 S4's finding): TextEdit's document read as one 200-character line, so a task about "the
third line" had to be guessed. A multi-line text area shows its lines (up to MAX_AREA_VALUE characters); its digest, and
so every readback that compares typed text, is computed as before; other fields are unchanged."""
from pebble import ax_worker


def el(role, value):
    return {'role': role, 'path': 'w/x', 'enabled': True, 'value': value}


def test_a_text_area_keeps_its_lines_and_its_digest():
    doc = 'Shopping\r\nbread\nmilk\rthird line here\x00'
    area = ax_worker.sanitize_element(el('AXTextArea', doc))
    assert area['value'] == 'Shopping\nbread\nmilk\nthird line here ' and area['value_truncated'] is False
    flat = ax_worker.clip(doc, ax_worker.MAX_TEXT * 4)
    assert '\n' not in flat and area['value_sha256'] == ax_worker.sha256(flat), 'the digest as before'
    assert ax_worker.sanitize_element(area) == area, 'idempotent'
    field = ax_worker.sanitize_element(el('AXTextField', 'one\ntwo' + 'x' * 400))
    assert '\n' not in field['value'] and len(field['value']) == ax_worker.MAX_VALUE, 'other fields unchanged'
