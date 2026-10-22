"""Phase 19 trial 3 finding: the per-UID process count behind a job's `nproc` limit.

`proc_listpids(PROC_RUID_ONLY, uid, NULL, 0)` is a size query: the kernel ignores the filter and answers the
system-wide process count plus 20. Used as "this UID's processes", it let `nproc: 8` allow every process of
the other users on the Mac as extra headroom (287 measured on 2026-10-13), so the Phase 14 fork-bomb test
flaked whenever system daemons were busy. The count now reads the filtered list.
"""
import os
import re
import subprocess
import sys

import pytest

from pebble import job_shim
from test_job_shim import assert_scoped, make_job, run_to_end
from test_job_shim import macos as sandboxed_macos

macos = pytest.mark.skipif(sys.platform != 'darwin', reason='libproc and per-UID RLIMIT_NPROC are macOS')


def _ruid_count_from_ps():
    out = subprocess.run(['/bin/ps', '-axo', 'ruid='], capture_output=True, text=True, check=True).stdout
    return sum(1 for value in out.split() if int(value) == os.getuid())


@macos
def test_the_pid_count_is_this_users_processes_not_the_systems():
    lib = job_shim._libproc_handle()
    assert lib is not None
    count = job_shim._count_pids()
    system_estimate = lib.proc_listpids(1, 0, None, 0) // 4  # PROC_ALL_PIDS size query: everyone's, plus 20
    ps_count = _ruid_count_from_ps()
    others = system_estimate - 20 - ps_count  # root and service accounts always run processes on macOS
    assert others > 30, (system_estimate, ps_count)
    # Processes come and go between the two reads; the old answer differed by every other user's process.
    assert abs(count - ps_count) <= 25, (count, ps_count)
    assert count < system_estimate - others // 2, (count, system_estimate, others)


@macos
def test_the_pid_count_survives_a_list_larger_than_the_first_buffer(monkeypatch):
    real = job_shim._libproc_handle()

    class Growing:
        """Reports a full first buffer, as the kernel does when the list does not fit."""

        def __init__(self):
            self.calls = []

        def proc_listpids(self, kind, arg, buf, size):
            self.calls.append(size)
            written = real.proc_listpids(kind, arg, buf, size)
            return size if len(self.calls) == 1 else written

    fake = Growing()
    monkeypatch.setattr(job_shim, '_libproc_handle', lambda: fake)
    count = job_shim._count_pids()
    assert len(fake.calls) == 2 and fake.calls[1] > fake.calls[0]
    assert abs(count - _ruid_count_from_ps()) <= 25


@sandboxed_macos
def test_a_fork_bomb_stops_near_its_nproc_headroom(tmp_path):
    src = ("import os, time\n"
           "n = 0\n"
           "try:\n"
           "    for _ in range(300):\n"
           "        if os.fork() == 0:\n"
           "            time.sleep(20); os._exit(0)\n"
           "        n += 1\n"
           "except OSError:\n"
           "    print('NPROC_STOP', n, flush=True)\n")
    job = make_job(tmp_path, src, limits={'nproc': 8, 'timeout': 20})
    shim = run_to_end(job)
    out = shim.handle.read_spool(0, 65536).decode('utf-8', 'replace')
    found = re.search(r'NPROC_STOP (\d+)', out)
    # Headroom is 8 plus whatever this user's other processes free meanwhile; the old count gave every
    # other user's processes as well (about 290 forks on the day it was found).
    assert found and int(found.group(1)) < 150, out
    assert shim.exited()['contained'] is True
    assert_scoped(job)
