"""Phase 45 (1.9.5, F055): a question about Pebble itself gets a short guide in the system prompt (above 8K), and the
guide stays true to the app: every Settings tab, menu item and key it names is the app's own."""
import re
from pathlib import Path

import pytest

from pebble import runner

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('text', [
    'In one sentence, how do I open the full workspace from this companion?',
    'What can you do?',
    'How do I turn on Stealth Lockdown?',
    'where are the settings for memory',
    'Does Pebble have keyboard shortcuts?',
])
def test_questions_about_pebble_are_recognised(text):
    assert runner.SELF_QUESTION.search(text)


@pytest.mark.parametrize('text', [
    'Update settings.json with the new key',
    'Sort this list in Python',
    'Fix the failing test in parser.py',
    'On YouTube, search for the navior song and play it',
])
def test_other_requests_are_left_alone(text):
    assert not runner.SELF_QUESTION.search(text)


def test_every_settings_tab_named_is_a_real_tab():
    vue = (ROOT / 'src/components/AdvancedSettings.vue').read_text()
    labels = dict(re.findall(r'^\s+(\w+): "([^"]+)",$', vue[vue.index('const TAB_LABELS'):vue.index('};', vue.index('const TAB_LABELS'))], re.M))
    tabs = vue[vue.index('const tabs = computed'):vue.index(']);', vue.index('const tabs = computed'))]
    shown = [labels[t] for t in labels if f'"{t}"' in tabs and t != 'mods']  # Views shows only in developer mode
    guide = runner.PEBBLE_GUIDE
    assert len(shown) >= 8, shown
    for label in shown:
        assert label in guide, label


def test_every_key_and_menu_item_named_exists():
    shell = (ROOT / 'src-tauri/src/main.rs').read_text()
    for key in ('CommandOrControl+Shift+Space', 'Control+Alt+Command+S', 'Control+Alt+Command+H'):
        assert f'register("{key}")' in shell, key
    for item, keys in (('New Conversation', 'CmdOrCtrl+N'), ('Command Palette…', 'CmdOrCtrl+K'),
                       ('Find Conversations…', 'CmdOrCtrl+F'), ('Show Task Activity', 'CmdOrCtrl+J'),
                       ('Show or Hide Details', 'CmdOrCtrl+Shift+D'), ('Settings…', 'CmdOrCtrl+,')):
        assert f'"{item}",Some("{keys}")' in shell, item
    assert '"menu:companion","Floating Companion"' in shell
    pet = (ROOT / 'src/Pet.vue').read_text()
    assert 'aria-label="Open full workspace"' in pet and 'Connect a project' in pet
    guide = runner.PEBBLE_GUIDE
    for words in ('Open full workspace', 'Window > Floating Companion', 'Connect a', "Pebble's Toolbox",
                  "Pebble's Workshop", 'Stealth Lockdown', 'Screen control', 'Take screenshots'):
        assert words in guide, words
    assert 'not from a camera' in guide  # the honest line (public wording rule)
