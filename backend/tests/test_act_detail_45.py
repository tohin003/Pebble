"""Phase 45 (1.9.14, F204 from audit round R7): Activity says what an app act did."""
from pebble import run_activity as ra


def test_an_act_row_names_its_target_and_its_change():
    assert ra.act_detail({'act_target': 'C', 'act_action': 'press', 'act_verdict': 'occurred',
                          'change_before': '100×10', 'change_after': '100×'}) == 'Pressed “C”: 100×10 → 100×'
    assert ra.act_detail({'act_target': 'd5', 'act_action': 'click_at', 'act_verdict': 'not_observed'}) == \
        'Clicked “d5”: nothing changed'
    assert ra.act_detail({'act_target': 'e4', 'act_action': 'press', 'act_verdict': 'occurred'}) == 'Pressed “e4”.'
    assert ra.act_detail({'act_target': None}) is None
    assert 'AS act_target' in ra.TOOLS_SQL and "'$.under_point.label'" in ra.TOOLS_SQL
