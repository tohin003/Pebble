"""Execution platform providers (Phase 14, X02).

Only macOS with the system sandbox-exec binary has confinement evidence. Linux
and Windows always refuse, and so does any other platform: no isolation was
verified there, so reviewed execution stays disabled rather than running
unconfined. A supported entry means only that the confinement primitive is
present; the containment canary, profiles and governors are still enforced by
their own owners before any job starts. Registry entries are host data; source
text, settings and model output cannot add a platform.
"""

import os
import sys

from .policy import PolicyError

SANDBOX_EXEC = '/usr/bin/sandbox-exec'
REASON = 'no verified isolation on this platform; execution stays disabled'
MISSING = 'sandbox-exec is not available on this Mac; execution stays disabled'
KINDS = ('command', 'tests', 'git', 'install', 'mcp_stdio')


def _sandbox_exec_present():
    try:
        return os.path.isfile(SANDBOX_EXEC) and not os.path.islink(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK)
    except (OSError, ValueError):
        return False


def provider(platform=None):
    """The registry entry for platform (default: the running sys.platform).

    {'platform', 'supported': bool, 'confinement': 'sandbox-exec' | None, 'reason': str | None}
    """
    platform = sys.platform if platform is None else platform
    if not isinstance(platform, str) or not 1 <= len(platform) <= 64:
        return {'platform': None, 'supported': False, 'confinement': None, 'reason': REASON}
    if platform == 'darwin':
        if _sandbox_exec_present():
            return {'platform': 'darwin', 'supported': True, 'confinement': 'sandbox-exec', 'reason': None}
        return {'platform': 'darwin', 'supported': False, 'confinement': None, 'reason': MISSING}
    return {'platform': platform, 'supported': False, 'confinement': None, 'reason': REASON}


def require(kind, platform=None):
    """The supported entry for one execution kind, or PolicyError with the refusal reason."""
    if kind not in KINDS:
        raise PolicyError('Unknown execution kind; execution stays disabled.')
    entry = provider(platform)
    if not entry['supported']:
        raise PolicyError(entry['reason'])
    return {**entry, 'kind': kind}
