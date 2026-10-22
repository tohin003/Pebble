"""Small structured worker errors, shared by source and frozen release entrypoints."""
import json
from pathlib import Path


def extract(flag, path):
    try:
        if flag == '--extract-image':
            from .images import parse_image
            result = parse_image(Path(path))
        else:
            from .attachments import parse_file
            result = parse_file(Path(path))
    except PermissionError:
        print(json.dumps({'error': 'Pebble could not access this file. Choose it again using Attach files.'}))
        raise SystemExit(1)
    except ValueError as exc:
        print(json.dumps({'error': str(exc)}))
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        print(json.dumps({'error': 'Could not decode this file. It may be damaged or use an unsupported encoding.'}))
        raise SystemExit(1)
    print(json.dumps(result))
