"""Phase 45 (1.9.11, F177, measured in .runtime/phase45-dev/native/f177-1.9.10.jsonl): with another app active, the
companion's message box keeps the keyboard (by design, F146), and ⌘Q quit Pebble, with any running task, while the menu
bar named that app. The shell drops ⌘Q, ⌘H and ⌥⌘H while Pebble is not the active app; the Dock's Quit, the menu-bar
icon's Quit and a logout still quit (none of them is a key event)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / 'src-tauri/src/main.rs').read_text()


def guard():
    return re.search(r'\nmod quit_guard \{\n(.*?)\n\}\n', SHELL, re.S).group(1)


def test_the_guard_is_installed_at_launch_on_macos():
    setup = SHELL[SHELL.index('.setup(|app| {'):]
    mac = setup[:setup.index('let _ = app.global_shortcut()')]
    assert '#[cfg(target_os="macos")]' in mac and 'quit_guard::install(); // F177' in mac


def test_it_drops_only_command_q_and_the_hide_keys_and_only_while_pebble_is_not_active():
    body = guard()
    assert 'addLocalMonitorForEventsMatchingMask: KEY_DOWN, handler: &*block' in body
    # measured on 1.9.11's first build: after a click in the companion NSApp.isActive is true while Finder owns the
    # menu bar; the guard asks whose menu bar it is
    assert 'if guarded(code, flags) && !menu_bar_ours() {' in body and 'app_active' not in body
    assert 'msg_send![workspace, menuBarOwningApplication]' in body and 'pid == std::process::id() as i32' in body
    assert ('(code == KEY_Q && mods == COMMAND) || (code == KEY_H && (mods == COMMAND || mods == COMMAND | OPTION))'
            in body)
    for constant in ('const KEY_DOWN: u64 = 1 << 10;', 'const SHIFT: usize = 1 << 17;', 'const CONTROL: usize = 1 << 18;',
                     'const OPTION: usize = 1 << 19;', 'const COMMAND: usize = 1 << 20;', 'const KEY_H: u16 = 4;',
                     'const KEY_Q: u16 = 12;'):
        assert constant in body, constant
    # every other key event passes through unchanged (Stealth Lockdown's ⌃⌥⌘H is a global shortcut and never seen here)
    assert body.count('return std::ptr::null_mut();') == 1 and 'event.as_ptr()' in body


def test_the_other_ways_to_quit_are_untouched():
    assert '"quit"=>request_quit(app)' in SHELL  # the menu-bar icon's Quit Pebble
    assert ('tauri::RunEvent::ExitRequested { api, code: None, .. } if !QUIT_CONFIRMED.load(Ordering::SeqCst) => '
            '{ api.prevent_exit(); request_quit(app); }') in SHELL  # the Dock's Quit, ⌘Q in Pebble, a logout
