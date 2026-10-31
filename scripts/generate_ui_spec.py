"""Phase 46 (1.9.29, D206, w24-genui-a): write pebble-ui-spec v1 from the code that builds what it describes.

The JSON Schema comes from backend/pebble/ui_spec.py, which derives the work view's part from work_view.SPEC and lists
every native kind artifact_schema.py defines; the fixtures come from work_view's own builders. So the published spec
cannot drift from Pebble: regenerate after a change there, and --check (run by test_ui_spec_46) fails when the files
are stale. No model, no network.

    python scripts/generate_ui_spec.py           # writes docs/ui-spec/pebble-ui-spec-v1.schema.json and fixtures.json
    python scripts/generate_ui_spec.py --check   # exits 1, naming the stale files, when they differ
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(ROOT / 'backend'))

from pebble import ui_spec  # noqa: E402

FOLDER = ROOT / 'docs' / 'ui-spec'
FILES = {'pebble-ui-spec-v1.schema.json': ui_spec.schema, 'fixtures.json': ui_spec.fixtures}


def rendered():
    """{file name: its text}: sorted keys, two-space indents, one trailing newline, UTF-8 as written."""
    return {name: json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False) + '\n' for name, build in FILES.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--check', action='store_true', help='fail when the written files are not up to date')
    args = parser.parse_args(argv)
    texts = rendered()
    if args.check:
        stale = [name for name, text in texts.items()
                 if not (FOLDER / name).is_file() or (FOLDER / name).read_text(encoding='utf-8') != text]
        if stale:
            print('Stale: ' + ', '.join(stale) + '. Run python scripts/generate_ui_spec.py.', file=sys.stderr)
            return 1
        print('pebble-ui-spec v1 is up to date.')
        return 0
    FOLDER.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (FOLDER / name).write_text(text, encoding='utf-8')
    print('Wrote ' + ', '.join(str((FOLDER / name).relative_to(ROOT)) for name in texts) + '.')
    summaries = ui_spec.summary_only()
    if summaries:
        print('Published with their summary only (describe them in ui_spec.NATIVE): ' + ', '.join(summaries) + '.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
