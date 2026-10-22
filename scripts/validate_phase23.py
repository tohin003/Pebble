#!/usr/bin/env python3
"""Phase 23 service validator: the Phase 22 campaign, with the release identity taken from the checkout.

validate_phase22.py pins RELEASE = '0.6.0', so it cannot pass once the release moves on. It is retired from Phase 23
on (scripts/verify_harness_candidate.py RETIRED, the person's decision of 2026-10-14, the same lifecycle as the
phase14-upgrade retirement) and superseded by this suite. It runs the same 46 checks with the same scenarios and
fixtures, unchanged (it imports them), except:
  - RELEASE is pebble.VERSION as the checkout declares it, and the release-identity checks still require every
    declaration and the running service to agree with it;
  - the two check names that spelled out 0.6.0 name the checkout's version instead.
Usage is validate_phase22.py's: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from validate_phase21 import checkout_version  # noqa: E402


def _private_phase22():
    """A private instance of validate_phase22: adopting it never changes the module other code imports."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_phase22_for_phase23', HERE / 'validate_phase22.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phase22 = _private_phase22()

RENAMED = {
    'v1_every_release_declaration_names_0_6_0_as_pebble_version':
        'v1_every_release_declaration_names_the_checkout_version_as_pebble_version',
    'v2_the_running_service_reports_release_0_6_0':
        'v2_the_running_service_reports_the_checkout_release',
}


def adopt():
    """Bind the Phase 22 campaign to the checkout's release and to the version-neutral names. Idempotent."""
    phase22.RELEASE = checkout_version()
    rename = lambda name: RENAMED.get(name, name)  # noqa: E731
    phase22.SCENARIOS = {key: type(names)(rename(n) for n in names) for key, names in phase22.SCENARIOS.items()}
    phase22.PLANNED = tuple(rename(n) for n in phase22.PLANNED)
    digests = phase22.checkout_digests
    if not getattr(digests, 'phase23_includes_self', False):
        def with_self():
            found = digests()
            found['scripts/validate_phase23.py'] = phase22.sha((HERE / 'validate_phase23.py').read_bytes())
            return found
        with_self.phase23_includes_self = True
        phase22.checkout_digests = with_self
    check = phase22.Campaign.check
    if not getattr(check, 'phase23_renames', False):
        def renamed(self, name, fn):
            return check(self, rename(name), fn)
        renamed.phase23_renames = True
        phase22.Campaign.check = renamed
    return phase22


def main(argv=None):
    return adopt().main(argv)


if __name__ == '__main__':
    sys.exit(main())
