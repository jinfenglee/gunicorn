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
