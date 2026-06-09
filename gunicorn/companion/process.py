#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.


import hashlib
import json

# Public states, mimicking ``supervisorctl status``. The manager never
# exposes EXITED/FATAL/UNKNOWN; an exited companion is either STOPPED (manual)
# or BACKOFF (waiting to restart).
STOPPED = "STOPPED"
STARTING = "STARTING"
RUNNING = "RUNNING"
BACKOFF = "BACKOFF"
STOPPING = "STOPPING"

PUBLIC_STATES = (STOPPED, STARTING, RUNNING, BACKOFF, STOPPING)


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
        blob = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _target_key(target):
        if callable(target):
            mod = getattr(target, "__module__", "")
            qual = getattr(target, "__qualname__", repr(target))
            return "%s:%s" % (mod, qual)
        return str(target)

    def __repr__(self):
        return "<CompanionConfig %s>" % self.name
