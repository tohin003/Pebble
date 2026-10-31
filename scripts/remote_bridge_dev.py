#!/usr/bin/env python3
"""Phase 46 (1.9.29, D214): Pebble Remote's bridge on loopback, for the iOS side's conformance.py and its app tests.

    cd backend && <Pebble's backend Python> ../scripts/remote_bridge_dev.py [--home DIR] [--folders-home DIR]
        [--port N] [--pair-confirm auto|ask|refuse] [--real-screen]
    python3 -I <PebbleRemote>/bridge-mock/conformance.py --link '<PAIRING_LINK>' --endpoint 127.0.0.1:<PORT>

What runs: a real Pebble App in a test home (a new temporary folder unless --home names one) with a scripted model
that answers "ok" to every message, so conformance's one message gets its short reply with no model and no network;
the bridge on 127.0.0.1 only, its pairing links listing 127.0.0.1; remote.folders listing a test tree (--folders-home,
or a folder in the test home), never your home folder; screenshots of a test pattern unless --real-screen.

stdout: HOME=, PORT=, PIN=, BID=, PAIRING_LINK= and READY=1; then PAIRING_CODE= whenever a pairing waits for the Mac's
confirmation (X-1), which --pair-confirm gives: auto (after 1.5 s, as a person would; the default), ask (type y or n),
or refuse (for conformance's --expect-pair-refused run). SIGUSR1 prints a new PAIRING_LINK=. Ctrl-C stops.
"""

import argparse
import copy
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
SCREENS = ((1512, 982), (2560, 1440))  # a MacBook's built-in screen and an external display, as test patterns


def scripted(app):
    """Every message gets "ok" from a scripted model, as evaluation._install_scripted does for Pebble's own tests."""
    from pebble.evaluation import SCRIPTED_PROFILE
    app.store.set_setting('model_path', '')
    app.store.set_setting('model_profiles', [copy.deepcopy(SCRIPTED_PROFILE)])
    app.model.select(SCRIPTED_PROFILE['id'])

    def complete(messages, tools, cancel, token, deliberate=False):
        if cancel.is_set():
            raise InterruptedError('Stopped by you.')
        token('ok')
        return {'role': 'assistant', 'content': 'ok'}
    app.model.complete = complete


def pattern_capture(argv):
    """screencapture's stand-in: colour bars for each display asked for, never this Mac's screen."""
    from PIL import Image, ImageDraw
    names = [arg for arg in argv[1:] if arg.endswith('.jpg')]
    wanted = int(argv[argv.index('-D') + 1]) - 1 if '-D' in argv else None
    for index, (width, height) in enumerate(SCREENS):
        if wanted is not None and index != wanted:
            continue
        image = Image.new('RGB', (width, height), (120, 140, 120))
        draw = ImageDraw.Draw(image)
        for bar, colour in enumerate(((236, 239, 233), (92, 122, 96), (40, 44, 40), (200, 120, 90))):
            draw.rectangle((bar * width // 4, 0, (bar + 1) * width // 4, height // 2), fill=colour)
        image.save(names[0] if wanted is not None else names[index], 'JPEG', quality=80)


def start(home, folders_home=None, port=0, confirm='auto', delay=1.5, real_screen=False, say=print):
    """(app, hub): a test-home Pebble with Remote on, on loopback. `confirm` answers each pairing for the person."""
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from pebble.app import App
    from pebble.remote import RemoteHub
    home = Path(home)
    hub, answered = RemoteHub(), set()

    def answer(code):
        if confirm == 'auto':
            time.sleep(delay)
            yes = True
        elif confirm == 'refuse':
            yes = False
        else:
            yes = input('Pairing code %s. Does the phone show the same six digits? [y/n] ' % code).strip().lower() \
                in ('y', 'yes')
        try:
            hub.remote_pair_confirm(yes)
        except ValueError:
            pass  # the phone left, or the two minutes ended, before the answer

    def emit(name, data):
        hub.tee(name, data)
        code = (data.get('confirm') or {}).get('code') if name == 'remote_state' else None
        if code and code not in answered:
            answered.add(code)
            say('PAIRING_CODE=' + code)
            threading.Thread(target=answer, args=(code,), name='remote-dev-confirm', daemon=True).start()

    app = App(home / 'data', home / 'resources', emit)
    scripted(app)
    tree = Path(folders_home) if folders_home else home / 'folders'
    tree.mkdir(parents=True, exist_ok=True)
    hub.attach(app, home / 'data', emit, name='Pebble dev Mac', folders_home=str(tree), link_loopback=True,
               address_plan=lambda: ([], []), port=port, capture=None if real_screen else pattern_capture,
               screen_allowed=None if real_screen else True)
    hub.remote_set('enabled', True)
    return app, hub


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pebble Remote's bridge on loopback with a test home, for conformance.")
    parser.add_argument('--home', help='the test home (default: a new temporary folder)')
    parser.add_argument('--folders-home', help='the tree remote.folders lists as home (default: a folder in --home)')
    parser.add_argument('--port', type=int, default=0, help='0 (the default) is any free port')
    parser.add_argument('--pair-confirm', choices=('auto', 'ask', 'refuse'), default='auto')
    parser.add_argument('--real-screen', action='store_true', help="remote.screenshot takes this Mac's screen")
    args = parser.parse_args(argv)
    home = Path(args.home or tempfile.mkdtemp(prefix='pebble-remote-dev-'))
    app, hub = start(home, args.folders_home, args.port, args.pair_confirm, real_screen=args.real_screen,
                     say=lambda line: print(line, flush=True))
    bridge = hub.bridge
    for line in ('HOME=%s' % home, 'PORT=%d' % bridge.port, 'PIN=' + bridge.pin, 'BID=' + bridge.bid):
        print(line, flush=True)
    stop = threading.Event()

    def link(*_):
        print('PAIRING_LINK=' + hub.remote_pair_start()['link'], flush=True)
    signal.signal(signal.SIGUSR1, lambda *_: threading.Thread(target=link, daemon=True).start())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    link()
    print('READY=1', flush=True)
    try:
        while not stop.wait(1):
            pass
    finally:
        hub.close()
        app.close()
    return 0


if __name__ == '__main__':
    os.umask(0o077)
    sys.exit(main())
