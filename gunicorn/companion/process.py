#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.


import hashlib
import json
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


class CompanionConfig:
    """Validated, normalized config for a single companion.

    Built from one entry of ``companion_workers`` with global defaults already
    applied. ``config_hash`` is a stable digest of every field; the manager
    restarts a companion whenever its hash changes on reread.
    """

    def __init__(
        self,
        name,
        target,
        cwd=None,
        env=None,
        stop_signal="SIGTERM",
        stop_timeout=60,
        reload_timeout=60,
        stdout=None,
        stderr=None,
        startsecs=1,
    ):
        self.name = name
        self.target = target
        self.cwd = cwd
        self.env = dict(env or {})
        self.stop_signal = stop_signal
        self.stop_timeout = stop_timeout
        self.reload_timeout = reload_timeout
        self.stdout = stdout
        self.stderr = stderr
        self.startsecs = startsecs

    def to_dict(self):
        return {
            "name": self.name,
            "target": self.target,
            "cwd": self.cwd,
            "env": self.env,
            "stop_signal": self.stop_signal,
            "stop_timeout": self.stop_timeout,
            "reload_timeout": self.reload_timeout,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "startsecs": self.startsecs,
        }

    @property
    def config_hash(self):
        # Sort keys so dict ordering never changes the digest. A callable
        # target has no stable repr across runs, so use its qualified name.
        data = self.to_dict()
        data["target"] = self._target_key(self.target)
        payload = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _target_key(target):
        if callable(target):
            module = getattr(target, "__module__", "")
            qualified_name = getattr(target, "__qualname__", repr(target))
            return "%s:%s" % (module, qualified_name)
        return str(target)

    def __repr__(self):
        return "<CompanionConfig %s>" % self.name


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
        if self.state == State.BACKOFF and self.next_retry_at is not None:
            seconds_left = max(0, int(self.next_retry_at - now))
            return "exited with %s, retrying in %ds" % (self._exit_status(), seconds_left)
        if self.state == State.BACKOFF:
            return "exited with %s" % self._exit_status()
        if self.state == State.STOPPED and self.manual_stop:
            return "stopped manually"
        if self.state == State.STOPPED and self.exited_at is not None:
            return "exited with %s" % self._exit_status()
        if self.state == State.STOPPED:
            return "not started"
        return ""

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
