# -*- mode: python ; coding: utf-8 -*-
# Pebble's frozen backend (PyInstaller onedir), built by scripts/build_candidate_app.py from the build environment
# that scripts/package_backend.py creates from backend/requirements.lock.
#
# V1-048: every path is relative to this file (SPECPATH is this build/ folder), so any clean clone builds. The
# historical .runtime/pebble-backend.spec is kept unchanged only because earlier phases' proofs hash it.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = os.path.dirname(os.path.abspath(SPECPATH))
BACKEND = os.path.join(ROOT, 'backend')

# V1-076 and V1-143: AgentGuard code Pebble never imports stays out of the app: the daemon's web app (FastAPI,
# Starlette and uvicorn), the command line, the Claude Code adapter, the benchmark runner and the census.
AGENTGUARD_UNUSED = ('agentguard.daemon.app', 'agentguard.daemon.__main__', 'agentguard.cli', 'agentguard.adapters',
                     'agentguard.bench', 'agentguard.census')
# Never collected: the web stack, developer tools, and pillow-heif with its GPL/LGPL libraries (HEIC is decoded by
# macOS itself, V1-045).
EXCLUDED = AGENTGUARD_UNUSED + ('fastapi', 'starlette', 'uvicorn', 'httpx', 'httpcore', 'anyio', 'h11', 'typer',
                                'click', 'rich', 'pygments', 'markdown_it', 'pytest', '_pytest', 'pluggy', 'ruff',
                                'opentelemetry', 'pillow_heif', '_pillow_heif',
                                # Phase 24 (parent, trial 2): jsonschema's own benchmark suite is not Pebble's and never runs.
                                'jsonschema.benchmarks',
                                # Phase 26: urllib3's optional `backports.zstd` import resolved to setuptools' vendored
                                # backports, which pulled all of setuptools (and its runtime hook) into the app without
                                # its licence notice. Nothing Pebble runs imports any of these.
                                'setuptools', 'pkg_resources', '_distutils_hack', 'distutils')
LANGUAGES = ('javascript', 'typescript', 'tsx', 'go', 'rust', 'java', 'ruby')


def wanted(name):
    return not any(name == unused or name.startswith(unused + '.') for unused in AGENTGUARD_UNUSED)


datas = []
binaries = []
hiddenimports = ['tiktoken_ext.openai_public'] + ['tree_sitter_language_pack.bindings.' + name for name in LANGUAGES]
hiddenimports += collect_submodules('agentguard', filter=wanted)
hiddenimports += collect_submodules('jsonschema', filter=lambda name: not name.startswith('jsonschema.benchmarks'))
hiddenimports += collect_submodules('PIL')
tmp_ret = collect_all('tzdata')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# Phase 24 (parent, trial 2): certifi's metadata ships so the bundled CA store's version is checkable (V1-024).
datas += copy_metadata('certifi')


a = Analysis(
    [os.path.join(BACKEND, 'main.py')],
    pathex=[os.path.join(BACKEND, 'vendor')],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(EXCLUDED),
    noarchive=False,
    optimize=0,
)
# Phase 24 (parent, trial 2): PyInstaller's jsonschema hook collects every data file, benchmark fixtures included.
a.datas = [entry for entry in a.datas if not entry[0].startswith('jsonschema/benchmarks')]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pebble-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # Phase 21: UPX-packed Mach-O files cannot be code-signed (D20 §1.6); UPX was never applied
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,  # Phase 21: UPX-packed Mach-O files cannot be code-signed (D20 §1.6); UPX was never applied
    upx_exclude=[],
    name='pebble-backend',
)
