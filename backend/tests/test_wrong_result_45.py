"""Phase 45 (1.9.14, F206 and F207 from audit round R8): a reply admitting a wrong move is held once while the window
is still granted, and an act row names the press with what changed, also when the window's rows moved in the tree."""
from pebble import run_activity as ra, runner
from pebble import screen_session as ss


def test_a_reply_that_admits_a_wrong_result_is_caught():
    said = ("I moved the white king's pawn one square to e3 instead of two; the computer has responded with ...e5. "
            "I didn't make further moves.")
    assert runner.WRONG_RESULT.search(said)
    for fine in ('Done: the white pawn is on e4 and Black answered e5.', 'I pressed the 5 key and the equals key.'):
        assert not runner.WRONG_RESULT.search(fine), fine
    assert runner.WRONG_RESULT.search('I pressed the wrong key, so the display shows 12.')
    assert 'Take Back' in runner.WRONG_CHECK and 'say so in one line' in runner.WRONG_CHECK


def squares(*labels):
    return {'elements': [{'role': 'AXButton', 'title': label, 'label': label, 'path': [0, i], 'secure': False}
                         for i, label in enumerate(labels)]}


def test_rows_that_moved_in_the_tree_still_show_what_changed():
    before = squares('a1', 'white pawn, e2', 'e3', 'e4', 'black pawn, e7')
    after = {'elements': [dict(e, path=[1] + e['path']) for e in
                          squares('a1', 'e2', 'e3', 'white pawn, e4', 'black pawn, e5')['elements']]}  # a new place
    changes = ss.row_changes(before, after)
    assert all(set(c) == {'role', 'before', 'after'} for c in changes) and changes, changes
    words = [(c['before'], c['after']) for c in changes]
    assert ('white pawn, e2', 'e2') in words and ('e4', 'white pawn, e4') in words, words
    same = squares('a1', 'b1')
    assert ss.row_changes(same, squares('a1', 'b1')) == [], 'nothing changed'
    assert ss.row_changes(squares('100×10'), squares('100×')) == [{'role': 'AXButton', 'before': '100×10',
                                                                   'after': '100×'}], 'in place, as before'


def test_an_act_row_is_titled_by_its_press_with_the_change_as_its_detail():
    tool = {'act_target': 'e3', 'act_action': 'press', 'act_verdict': 'occurred',
            'change_before': 'white pawn, e2', 'change_after': 'e2'}
    assert ra.act_title(tool) == 'Pressed “e3”' and ra.act_change(tool) == 'white pawn, e2 → e2'
    assert ra.act_change({'act_verdict': 'not_observed'}) == 'Nothing changed.'
    assert ra.act_change({'act_verdict': 'occurred'}) == 'The window changed.'
    assert ra.act_title({'act_target': ''}) is None
    assert ra.act_change({'change_before': '', 'change_after': 'black pawn, e5'}) == '(nothing) → black pawn, e5'


def test_replies_that_say_the_task_was_not_done_count_as_not_finished():
    # audit round R8 (F214): each of these was kept in Past experiences as "Result: finished"
    for said in ("I couldn't create the PNG: Pebble's file-writing tools can write in its Workbench.",
                 "I couldn't draw the rectangle. The Preview window access granted for this session allows filling.",
                 "I still can't draw the rectangle.", "The queen’s pawn move was not made; the staged action was rejected.",
                 "I opened Calculator and pressed the 1 button, but I didn't complete the button sequence.",
                 # audit round R10 (F242): blocked, "has not moved", and a closing promise (F236)
                 "I’m blocked: Chess repeatedly rejects clicks on the board. The position remains unchanged, so the "
                 "queen’s pawn has not moved.", "Black answered. The rectangle hasn't been drawn yet.",
                 "The calculator had a previous value. I'll clear the calculator and enter 123 × 456 again."):
        assert runner.not_done(said), said
    for said in ('Done: the white pawn is on e4.', 'Calculator computed 123 × 456 = 56,088.',
                 "I opened the file. Later on, the app couldn't show thumbnails, which doesn't matter here.", '', None,
                 'Moved the pawn from d2 to d4; the board is not blocked.', 'Done: the rectangle is red and drawn.'):
        assert not runner.not_done(said), said


def test_an_act_row_names_two_changes_when_a_move_changes_two_squares():
    # audit round R9 (F222): "white pawn, e2 → e2" said nothing of where the pawn went
    tool = {'act_verdict': 'occurred', 'change_before': 'white pawn, e2', 'change_after': 'e2',
            'change2_before': 'e4', 'change2_after': 'white pawn, e4'}
    assert ra.act_change(tool) == 'white pawn, e2 → e2; e4 → white pawn, e4'
    assert "'$.changes[1].after'" in ra.TOOLS_SQL
