#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.


from __future__ import annotations

import importlib
import os
import time
from typing import TYPE_CHECKING, Callable, Iterable, Union

from gunicorn.companion.process import CompanionProcess, State

if TYPE_CHECKING:
    from gunicorn.companion.process import CompanionConfig


class CompanionManager:
    """Forks and supervises companion processes.

    Created by the arbiter after preload. Holds one ``CompanionProcess`` per
    configured companion and owns the fork lifecycle. This skeleton wires
    construction and single-companion spawn; reaping, backoff, the control
    socket, and the run loop arrive in later tasks.
    """

    def __init__(self, configs: Iterable[CompanionConfig], log):
        self.log = log
        self.pid = os.getpid()
        self.processes = {c.name: CompanionProcess(c) for c in configs}

    def spawn_process(self, proc: CompanionProcess) -> int:
        """Fork one companion.

        Parent records the pid and moves the companion to STARTING. Child
        resolves and runs the target, exiting the worker on any failure so a
        crashed companion never leaks back into the manager's control flow.
        """
        pid = os.fork()
        if pid != 0:
            proc.pid = pid
            proc.state = State.STARTING
            proc.started_at = time.time()
            proc.manual_stop = False
            self.log.info("companion %s started (pid %s)", proc.name, pid)
            return pid

        try:
            self._apply_environment(proc.config)
            self._redirect_output(proc.config)
            target = self._resolve_target(proc.config.target)
            target()
        except SystemExit:
            raise
        except BaseException:
            self.log.exception("companion %s crashed", proc.name)
            os._exit(1)
        os._exit(0)

    @staticmethod
    def _apply_environment(config: CompanionConfig) -> None:
        """Apply ``cwd`` and ``env`` in the child before running the target.

        cwd is changed first so a relative path in env (or the target itself)
        resolves against it. env is merged onto the inherited environment, not
        replaced, so the companion keeps the manager's variables.
        """
        if config.cwd:
            os.chdir(config.cwd)
        if config.env:
            os.environ.update(config.env)

    @staticmethod
    def _redirect_output(config: CompanionConfig) -> None:
        """Send the companion's stdout and stderr to its configured log files.

        By default a companion just inherits the manager's stdout/stderr, so
        leaving these unset (or ``"inherit"``) keeps that. Give a file path and
        we append the output there instead. For stderr you can also pass
        ``"stdout"`` to fold the two streams into one file.
        """
        out = CompanionManager._open_output(config.stdout)
        if out is not None:
            os.dup2(out, 1)
        if config.stderr == "stdout":
            os.dup2(1, 2)
        else:
            err = CompanionManager._open_output(config.stderr)
            if err is not None:
                os.dup2(err, 2)

    @staticmethod
    def _open_output(value):
        """Open one log file for writing, or return None to leave the stream
        as-is when the companion should keep inheriting it."""
        if value in (None, "inherit"):
            return None
        return os.open(value, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    @staticmethod
    def _resolve_target(target: Union[Callable, str]) -> Callable:
        """Return the zero-arg callable for a companion target.

        Accepts an already-callable target or a ``"module:attr"`` import
        string, e.g. ``"frappe_companions:start_rq_default"``.
        """
        if callable(target):
            return target
        module, sep, attr = target.partition(":")
        if not sep:
            raise ValueError("companion target %r must be 'module:callable'" % target)
        return getattr(importlib.import_module(module), attr)
