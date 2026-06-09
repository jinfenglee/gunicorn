#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import hashlib
import json


# Maps each optional companion field to the global setting build_companion_configs
# reads when a spec omits it. ``name`` and ``target`` are required per spec and
# have no global default, so they are filled directly instead of through here.
FIELD_DEFAULTS = {
    "cwd": "companion_cwd",
    "env": "companion_env",
    "stop_signal": "companion_stop_signal",
    "stop_timeout": "companion_stop_timeout",
    "reload_timeout": "companion_reload_timeout",
    "stdout": "companion_stdout",
    "stderr": "companion_stderr",
    "startsecs": "companion_startsecs",
}


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


def build_companion_configs(cfg):
    """Build a CompanionConfig list from ``cfg.companion_workers``.

    A spec missing ``name`` or ``target`` is rejected, since the manager has
    nothing to supervise without both.
    """
    configs = []
    for spec in cfg.companion_workers:
        if "name" not in spec or "target" not in spec:
            raise ValueError(
                "each companion worker needs 'name' and 'target': %s" % spec)
        fields = {field: spec.get(field, getattr(cfg, setting))
                  for field, setting in FIELD_DEFAULTS.items()}
        configs.append(
            CompanionConfig(name=spec["name"], target=spec["target"], **fields))
    return configs
