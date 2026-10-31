"""The person's time zone setting (TZ) for the commands Pebble runs for them (Phase 46).

Every launcher builds its environment from nothing, so a TZ the person (or a project's runner) set never reached the
tests, terminal commands and code Pebble ran: they ran in the Mac's zone while the person's own terminal and CI ran in
theirs. On the golden set every case sets TZ=UTC for every product; Codex's and Claude Code's shells inherit it, and
Pebble's RunTests ran the same tests at UTC+05:30. Only a well-formed value is passed on; nothing else is inherited."""

import os
import re

ZONE = re.compile(r':?[A-Za-z0-9_+\-/.,<>]{1,64}')


def person_zone():
    """{'TZ': value} when this process has a well-formed TZ, else {} (the Mac's own zone applies, as before)."""
    value = os.environ.get('TZ')
    return {'TZ': value} if isinstance(value, str) and ZONE.fullmatch(value) else {}
