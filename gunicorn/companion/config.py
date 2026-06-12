#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import hashlib
import json
import signal


# Maps each optional companion field to the global setting build_companion_configs
# reads when a spec omits it. ``name`` and ``target`` are required per spec and
# have no global default. ``restart_delay`` is global-only, so it is absent here.
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
        restart_delay=5,
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
        self.restart_delay = restart_delay

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


ALLOWED_SPEC_KEYS = {"name", "target"} | set(FIELD_DEFAULTS)


def _validate_stop_signal(stop_signal, name):
    """Reject a stop_signal that does not name a real signal.

    Caught here at build time so a typo like ``"SIGTRM"`` fails loudly when
    config is loaded or rereaded, rather than crashing the manager later when
    it tries to send the signal.
    """
    try:
        if isinstance(stop_signal, str):
            signal.Signals[stop_signal]
        else:
            signal.Signals(stop_signal)
    except (KeyError, ValueError):
        raise ValueError(
            "companion %s has unknown stop_signal %r" % (name, stop_signal))


def _load_companion_settings(cfg):
    """Return the ``companion_*`` settings from ``companion_config_file``, or
    ``{}`` when no dedicated file is configured."""
    path = getattr(cfg, "companion_config_file", None)
    if not path:
        return {}
    namespace = {}
    with open(path) as config_file:
        # The companion config file is trusted operator input, like the main
        # Gunicorn config; running it is the point.
        exec(compile(config_file.read(), path, "exec"), namespace)  # pylint: disable=exec-used
    return {name: value for name, value in namespace.items()
            if name.startswith("companion_")}


def build_companion_configs(cfg):
    """Build a CompanionConfig list from the companion settings.

    Settings come from ``companion_config_file`` when set, otherwise ``cfg``. A
    spec is rejected if it is missing ``name``/``target`` or carries an unknown
    key.
    """
    overrides = _load_companion_settings(cfg)

    def setting(name):
        return overrides.get(name, getattr(cfg, name))

    configs = []
    for spec in setting("companion_workers"):
        if "name" not in spec or "target" not in spec:
            raise ValueError(
                "each companion worker needs 'name' and 'target': %s" % spec)
        unknown = set(spec) - ALLOWED_SPEC_KEYS
        if unknown:
            raise ValueError(
                "unknown companion worker key(s) %s in %s"
                % (sorted(unknown), spec))
        fields = {field: spec.get(field, setting(global_setting))
                  for field, global_setting in FIELD_DEFAULTS.items()}
        _validate_stop_signal(fields["stop_signal"], spec["name"])
        configs.append(CompanionConfig(
            name=spec["name"], target=spec["target"],
            restart_delay=setting("companion_restart_delay"), **fields))
    return configs
