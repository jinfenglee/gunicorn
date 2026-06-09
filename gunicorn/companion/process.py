#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.


import time
from enum import Enum

from gunicorn.util import format_uptime


class State(str, Enum):
    """Public states, mimicking ``supervisorctl status``.

    The manager never exposes EXITED/FATAL/UNKNOWN; an exited companion is
    either STOPPED (manual) or BACKOFF (waiting to restart). Members subclass
    ``str`` so they compare and JSON-serialize as their plain value.
    """

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    BACKOFF = "BACKOFF"
    STOPPING = "STOPPING"


class CompanionProcess:
    """Runtime state for one companion, separate from its static config.

    Holds everything ``status`` needs: current public state, live pid, restart
    and exit counters, last exit info, and the ``manual_stop`` flag that keeps a
    user-stopped companion from auto-restarting.
    """

    def __init__(self, config):
        self.config = config
        self.state = State.STOPPED
        self.pid = None
        self.restart_delay = 5

        self.started_at = None
        self.exited_at = None
        self.next_retry_at = None
        self.stop_deadline = None

        self.restart_count = 0
        self.exit_count = 0
        self.kill_count = 0

        self.last_exit_code = None
        self.last_exit_signal = None

        self.manual_stop = False
        self.restart_pending = False

    @property
    def name(self):
        return self.config.name

    def uptime(self, now=None):
        """Seconds since this companion last started, or ``None`` if not up."""
        if self.state not in (State.RUNNING, State.STARTING) or self.started_at is None:
            return None
        return (now or time.time()) - self.started_at

    def description(self, now=None):
        """Human one-liner: state label plus runtime details."""
        now = now or time.time()
        label = self.state.lower()
        detail = self._detail(now)
        return "%s, %s" % (label, detail) if detail else label

    def _detail(self, now):
        if self.state == State.RUNNING:
            return "pid %s, uptime %s" % (
                self.pid,
                format_uptime(self.uptime(now) or 0),
            )
        if self.state == State.BACKOFF:
            return self._backoff_detail(now)
        if self.state == State.STOPPED:
            return self._stopped_detail()
        return ""

    def _backoff_detail(self, now):
        if self.next_retry_at is not None:
            seconds_left = max(0, int(self.next_retry_at - now))
            return "exited with %s, retrying in %ds" % (self._exit_status(), seconds_left)
        return "exited with %s" % self._exit_status()

    def _stopped_detail(self):
        if self.manual_stop:
            return "stopped manually"
        if self.exited_at is not None:
            return "exited with %s" % self._exit_status()
        return "not started"

    def _exit_status(self):
        if self.last_exit_signal is not None:
            return "signal %s" % self.last_exit_signal
        return "status %s" % self.last_exit_code

    def status_dict(self, now=None):
        """Machine-readable status entry for the JSON control protocol."""
        backoff = self.state == State.BACKOFF
        return {
            "name": self.name,
            "state": self.state,
            "pid": self.pid,
            "description": self.description(now or time.time()),
            "next_retry_at": self.next_retry_at if backoff else None,
            "restart_delay": self.restart_delay if backoff else None,
            "last_exit_code": self.last_exit_code if backoff else None,
        }

    def __repr__(self):
        return "<CompanionProcess %s %s>" % (self.name, self.state)
