"""Observe a child process leader's exit without reaping it.

While the leader is an unreaped zombie its PID, and so its process group ID,
stays reserved: a final killpg sweep of the group cannot reach a reused PID.
macOS Python has no os.waitid, so this uses a kqueue NOTE_EXIT filter, which
fires on exit and leaves the zombie for the caller's wait(). Registering on a
leader that has already exited raises ESRCH, which means the same thing here
because only the caller ever reaps it.
"""

import os
import select


class LeaderWatch:
    def __init__(self, pid):
        self.pid = pid
        self.done = False
        self._kq = None
        if hasattr(select, 'kqueue'):
            self._kq = select.kqueue()
            try:
                self._kq.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                fflags=select.KQ_NOTE_EXIT)], 0)
            except ProcessLookupError:
                self.done = True  # exited already; still unreaped until the caller waits

    @property
    def reliable(self):
        """True when exit can be observed without reaping (kqueue or waitid)."""
        return self._kq is not None or hasattr(os, 'waitid')

    def exited(self, timeout=0.0):
        """Wait up to timeout seconds for the leader's exit; never reaps it."""
        if self.done:
            return True
        if self._kq is not None:
            self.done = bool(self._kq.control(None, 1, timeout))
        elif hasattr(os, 'waitid'):
            self.done = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        return self.done

    def close(self):
        if self._kq is not None:
            self._kq.close()
            self._kq = None
