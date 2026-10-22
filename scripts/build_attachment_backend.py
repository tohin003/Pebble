"""Overlay attachment modules on the verified installed backend, preserving
unrelated modules and excluding concurrent runtime source work from this release.
Run with the same Python 3.14 interpreter used by the installed PyInstaller app.
"""
import hashlib
import argparse
import json
import marshal
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter
from PyInstaller.utils.osx import remove_signature_from_binary, fix_exe_for_code_signing

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline', type=Path, default=Path('/Applications/Pebble.app/Contents/Resources/resources/backend'), help='Backend directory from the verified 0.4.1 bundle (including _internal).')
BASE = parser.parse_args().baseline.resolve()
WORK = ROOT / '.runtime/attachment-release'
WORK.mkdir(parents=True, exist_ok=True)
OUTPUT = WORK / 'backend'
if OUTPUT.exists():
    shutil.rmtree(OUTPUT)
shutil.copytree(BASE, OUTPUT, symlinks=True)
binary = OUTPUT / 'pebble-backend'
original_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
expected = '28fb43a7337716326b5c32ad3eccd739e095b4515679f850044c78b104236cdd'
if original_sha != expected:
    raise SystemExit('The installed backend differs from the verified 0.4 baseline; no application was changed.')
archive = CArchiveReader(str(binary))
pyz = archive.open_embedded_archive('PYZ.pyz')
codes = {name: pyz.extract(name) for name, entry in pyz.toc.items() if entry[0] != 3}
baseline = dict(codes)
entries = [(name, None if entry[0] == 3 else ('__init__.py' if entry[0] == 1 else 'module.py'), 'PYMODULE') for name, entry in pyz.toc.items()]
changed = ['pebble.documents', 'pebble.images', 'pebble.attachments', 'pebble.attachment_worker', 'pebble.attachment_release']
for name in changed:
    path = ROOT / 'backend' / (name.replace('.', '/') + '.py')
    codes[name] = compile(path.read_text(), name.replace('.', '/') + '.py', 'exec')
    if name not in pyz.toc:
        entries.append((name, 'module.py', 'PYMODULE'))
heif = distribution('pillow-heif')
for relative in heif.files:
    path = Path(heif.locate_file(relative))
    if path.suffix == '.py' and str(relative).startswith('pillow_heif/'):
        name = str(relative).removesuffix('.py').replace('/', '.')
        if name.endswith('.__init__'):
            name = name.removesuffix('.__init__')
        codes[name] = compile(path.read_text(), str(relative), 'exec')
        entries.append((name, '__init__.py' if path.name == '__init__.py' else 'module.py', 'PYMODULE'))
    elif path.suffix in {'.so', '.dylib'}:
        destination = OUTPUT / '_internal' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(destination)], check=True)
    elif 'license' in str(relative).lower():
        destination = WORK / 'licenses' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
codes['pebble.legacy_entry'] = marshal.loads(archive.extract('main'))
entries.append(('pebble.legacy_entry', 'module.py', 'PYMODULE'))
pyz_path = WORK / 'PYZ.pyz'
ZlibArchiveWriter(str(pyz_path), entries, codes)

# Replace only the entrypoint dispatch to support the new file worker. The
# service protocol and App implementation continue to run their installed code.
main = compile('''import sys
if any(flag in sys.argv for flag in ('--extract-image', '--extract-file')):
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    from pebble.attachment_worker import extract
    flag = '--extract-image' if '--extract-image' in sys.argv else '--extract-file'
    extract(flag, sys.argv[sys.argv.index(flag) + 1])
else:
    from pebble.app import App
    from pebble.attachment_release import install
    install(App)
    from pebble.legacy_entry import main
    main()
''', 'main.py', 'exec')
raw_entries = []
for name, entry in archive.toc.items():
    _, _, _, compressed, typecode = entry
    blob = marshal.dumps(main) if name == 'main' else pyz_path.read_bytes() if name == 'PYZ.pyz' else archive.extract(name)
    raw_entries.append((name, blob, compressed, typecode))
raw_entries += [(option, b'', False, 'o') for option in archive.options]

class RawWriter(CArchiveWriter):
    def _write_entry(self, fp, entry):
        name, data, compressed, typecode = entry
        return self._write_blob(fp, data, name, typecode, compress=compressed)

pkg = WORK / 'backend.pkg'
RawWriter(str(pkg), raw_entries, 'Python')
offset = archive._start_offset
remove_signature_from_binary(str(binary))
with binary.open('r+b') as stream:
    stream.truncate(offset)
    stream.seek(offset)
    stream.write(pkg.read_bytes())
fix_exe_for_code_signing(str(binary))
subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(binary)], check=True)
check = CArchiveReader(str(binary)).open_embedded_archive('PYZ.pyz')
unchanged = [n for n in baseline if n not in changed]
assert all(baseline[n] == check.extract(n) for n in unchanged)
assert 'pebble.runtime' not in check.toc
manifest = {'baseline_sha256': original_sha, 'backend_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'changed_modules': changed, 'other_modules_identical': len(unchanged), 'concurrent_runtime_source_included': False}
(WORK / 'backend-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest, indent=2))
