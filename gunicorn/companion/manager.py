#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.


from __future__ import annotations

import importlib
import os
import signal
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

    def start_process(self, name: str):
        """Start a companion by name (the control ``start`` command).

        Follows the supervisor-style rules: a STOPPED or BACKOFF companion
        clears its ``manual_stop`` flag, drops any pending retry, and is spawned
        right away. RUNNING and STARTING are already-up, so they report success
        without doing anything. STOPPING is rejected so the caller polls status
        and retries once the old child is gone. Returns ``(ok, message)``.
        """
        proc = self.processes.get(name)
        if proc is None:
            return False, "unknown companion %s" % name
        if proc.state in (State.RUNNING, State.STARTING):
            return True, "%s already %s" % (name, proc.state.lower())
        if proc.state == State.STOPPING:
            return False, "%s is stopping; retry" % name
        proc.manual_stop = False
        proc.next_retry_at = None
        self.spawn_process(proc)
        return True, "%s started" % name

    def stop_process(self, name: str, now: float = None):
        """Stop a companion by name (the control ``stop`` command).

        Sets ``manual_stop`` so the companion will not auto-restart. A live
        companion (RUNNING or STARTING) is sent its ``stop_signal`` and moved
        to STOPPING with a ``stop_deadline``; the run loop reaps it, or SIGKILLs
        it once the deadline passes. BACKOFF just cancels the pending retry and
        settles in STOPPED. STOPPED and STOPPING are already-there success
        no-ops. Returns ``(ok, message)``.
        """
        proc = self.processes.get(name)
        if proc is None:
            return False, "unknown companion %s" % name
        proc.manual_stop = True
        if proc.state in (State.STOPPED, State.STOPPING):
            return True, "%s already %s" % (name, proc.state.lower())
        if proc.state == State.BACKOFF:
            proc.next_retry_at = None
            proc.state = State.STOPPED
            return True, "%s stopped" % name
        now = now or time.time()
        os.kill(proc.pid, self._signal_number(proc.config.stop_signal))
        proc.state = State.STOPPING
        proc.stop_deadline = now + proc.config.stop_timeout
        self.log.info("companion %s stopping (pid %s)", name, proc.pid)
        return True, "%s stopping" % name

    def restart_process(self, name: str, now: float = None):
        """Restart a companion by name (the control ``restart`` command).

        Always clears ``manual_stop`` so the companion comes back. A live
        companion (RUNNING or STARTING) is asked to stop -- it goes STOPPING
        with ``restart_pending`` set and a deadline based on ``reload_timeout``,
        and the reaper respawns it as soon as the old child exits. BACKOFF and
        STOPPED start again immediately. STOPPING is rejected so the caller
        retries. This never rereads config. Returns ``(ok, message)``.
        """
        proc = self.processes.get(name)
        if proc is None:
            return False, "unknown companion %s" % name
        if proc.state == State.STOPPING:
            return False, "%s is stopping; retry" % name
        proc.manual_stop = False
        if proc.state in (State.RUNNING, State.STARTING):
            now = now or time.time()
            proc.restart_pending = True
            os.kill(proc.pid, self._signal_number(proc.config.stop_signal))
            proc.state = State.STOPPING
            proc.stop_deadline = now + proc.config.reload_timeout
            self.log.info("companion %s restarting (pid %s)", name, proc.pid)
            return True, "%s restarting" % name
        proc.next_retry_at = None
        self.spawn_process(proc)
        return True, "%s started" % name

    @staticmethod
    def _signal_number(sig) -> int:
        """Resolve a stop signal to its number, e.g. ``"SIGTERM"`` -> 15.

        Accepts a signal name or a raw number and validates both against the
        real signal table, so a typo like ``"SIGTRM"`` fails loudly here rather
        than silently sending the wrong signal (or none).
        """
        try:
            return signal.Signals[sig] if isinstance(sig, str) else signal.Signals(sig)
        except (KeyError, ValueError):
            raise ValueError("unknown stop signal %r" % (sig,))

    def reap_processes(self) -> list:
        """Reap any companions that have exited and record their exit info.

        A companion runs as a forked child of the manager, so when it dies the
        kernel hands its exit status back to us as a zombie until we collect it
        with ``waitpid``. This method does that collecting, and is meant to be
        called once per run-loop tick (typically after a ``SIGCHLD``).

        ``waitpid(-1, WNOHANG)`` asks the kernel for any one dead child without
        blocking. It returns ``(pid, status)`` for a child it reaped, or
        ``(0, 0)`` when children are still alive but none have exited. Several
        companions can die between two ticks, so we loop until one of those two
        stop conditions is hit: ``(0, 0)`` (nothing more to reap right now) or
        ``ChildProcessError`` (no child processes exist at all).

        For each reaped pid we look up its companion, then in order: record the
        exit (signal or code, time, count), free the pid, and move it to its
        next public state via :meth:`handle_exit` -- STOPPED if it was stopped
        on purpose, otherwise BACKOFF for a later restart. Pids we don't
        recognise are ignored. Returns the list of companions reaped this call.
        """
        reaped = []
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            proc = self._process_by_pid(pid)
            if proc is not None:
                self._record_exit(proc, status)
                self.handle_exit(proc)
                reaped.append(proc)
        return reaped

    def handle_exit(self, proc: CompanionProcess, now: float = None) -> None:
        """Decide a companion's fate after it exits: restart, stop, or back off.

        A pending restart wins: the old child was asked to stop only so a fresh
        one could take its place, so it is respawned immediately. Otherwise a
        companion that was stopped on purpose settles in STOPPED and stays
        there, and any other exit is unexpected, so it enters BACKOFF and is
        scheduled to restart after a fixed ``restart_delay`` (no exponential
        backoff, no retry cap).
        """
        now = now or time.time()
        if proc.restart_pending:
            proc.restart_pending = False
            proc.restart_count += 1
            self.spawn_process(proc)
            return
        if proc.manual_stop:
            proc.state = State.STOPPED
            proc.next_retry_at = None
            return
        proc.state = State.BACKOFF
        proc.next_retry_at = now + proc.restart_delay
        self.log.info("companion %s exited, retrying in %ss",
                      proc.name, proc.restart_delay)

    def retry_backoff(self, now: float = None) -> list:
        """Respawn BACKOFF companions whose fixed retry delay has elapsed.

        Each retry bumps ``restart_count`` and re-forks the companion, which
        puts it back into STARTING. Returns the companions that were retried.
        """
        now = now or time.time()
        retried = []
        for proc in self.processes.values():
            if proc.state != State.BACKOFF or proc.next_retry_at is None:
                continue
            if now >= proc.next_retry_at:
                proc.restart_count += 1
                proc.next_retry_at = None
                self.spawn_process(proc)
                retried.append(proc)
        return retried

    def promote_running(self, now: float = None) -> list:
        """Move companions that survived ``startsecs`` from STARTING to RUNNING.

        A freshly spawned companion starts in STARTING. If it stays alive for
        its ``startsecs`` window it is considered up and becomes RUNNING; if it
        dies first, reaping handles it instead. Returns the promoted ones.
        """
        now = now or time.time()
        promoted = []
        for proc in self.processes.values():
            if proc.state != State.STARTING or proc.started_at is None:
                continue
            if now - proc.started_at >= proc.config.startsecs:
                proc.state = State.RUNNING
                self.log.info("companion %s running (pid %s)", proc.name, proc.pid)
                promoted.append(proc)
        return promoted

    def _process_by_pid(self, pid: int):
        for proc in self.processes.values():
            if proc.pid == pid:
                return proc
        return None

    @staticmethod
    def _record_exit(proc: CompanionProcess, status: int) -> None:
        """Store how a companion died: signal number or exit code, plus time.

        ``status`` is the packed value from ``waitpid``. ``WIFSIGNALED`` tells
        us a signal killed it, in which case ``WTERMSIG`` gives the signal
        number; otherwise it exited normally and ``WEXITSTATUS`` gives its exit
        code. Only one of the two is ever set, so the other is cleared.
        """
        if os.WIFSIGNALED(status):
            proc.last_exit_signal = os.WTERMSIG(status)
            proc.last_exit_code = None
        else:
            proc.last_exit_code = os.WEXITSTATUS(status)
            proc.last_exit_signal = None
        proc.exited_at = time.time()
        proc.exit_count += 1
        proc.pid = None

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
