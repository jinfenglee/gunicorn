#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import errno
import os
import signal
from unittest import mock

import gunicorn.app.base
import gunicorn.arbiter
from gunicorn.config import ReusePort


class DummyApplication(gunicorn.app.base.BaseApplication):
    """
    Dummy application that has a default configuration.
    """

    def init(self, parser, opts, args):
        """No-op"""

    def load(self):
        """No-op"""

    def load_config(self):
        """No-op"""


@mock.patch('gunicorn.sock.close_sockets')
def test_arbiter_stop_closes_listeners(close_sockets):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    listener1 = mock.Mock()
    listener2 = mock.Mock()
    listeners = [listener1, listener2]
    arbiter.LISTENERS = listeners
    arbiter.stop()
    close_sockets.assert_called_with(listeners, True)


@mock.patch('gunicorn.sock.close_sockets')
def test_arbiter_stop_child_does_not_unlink_listeners(close_sockets):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.reexec_pid = os.getpid()
    arbiter.stop()
    close_sockets.assert_called_with([], False)


@mock.patch('gunicorn.sock.close_sockets')
def test_arbiter_stop_parent_does_not_unlink_listeners(close_sockets):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.master_pid = os.getppid()
    arbiter.stop()
    close_sockets.assert_called_with([], False)


@mock.patch('gunicorn.sock.close_sockets')
def test_arbiter_stop_does_not_unlink_systemd_listeners(close_sockets):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.systemd = True
    arbiter.stop()
    close_sockets.assert_called_with([], False)


@mock.patch('gunicorn.sock.close_sockets')
def test_arbiter_stop_does_not_unlink_when_using_reuse_port(close_sockets):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.settings['reuse_port'] = ReusePort()
    arbiter.cfg.settings['reuse_port'].set(True)
    arbiter.stop()
    close_sockets.assert_called_with([], False)


@mock.patch('os.getpid')
@mock.patch('os.fork')
@mock.patch('os.execvpe')
def test_arbiter_reexec_passing_systemd_sockets(execvpe, fork, getpid):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.LISTENERS = [mock.Mock(), mock.Mock()]
    arbiter.systemd = True
    fork.return_value = 0
    getpid.side_effect = [2, 3]
    arbiter.reexec()
    environ = execvpe.call_args[0][2]
    assert environ['GUNICORN_PID'] == '2'
    assert environ['LISTEN_FDS'] == '2'
    assert environ['LISTEN_PID'] == '3'


@mock.patch('os.getpid')
@mock.patch('os.fork')
@mock.patch('os.execvpe')
def test_arbiter_reexec_passing_gunicorn_sockets(execvpe, fork, getpid):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    listener1 = mock.Mock()
    listener2 = mock.Mock()
    listener1.fileno.return_value = 4
    listener2.fileno.return_value = 5
    arbiter.LISTENERS = [listener1, listener2]
    fork.return_value = 0
    getpid.side_effect = [2, 3]
    arbiter.reexec()
    environ = execvpe.call_args[0][2]
    assert environ['GUNICORN_FD'] == '4,5'
    assert environ['GUNICORN_PID'] == '2'


@mock.patch('os.fork')
def test_arbiter_reexec_limit_parent(fork):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.reexec_pid = ~os.getpid()
    arbiter.reexec()
    assert fork.called is False, "should not fork when there is already a child"


@mock.patch('os.fork')
def test_arbiter_reexec_limit_child(fork):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.master_pid = ~os.getpid()
    arbiter.reexec()
    assert fork.called is False, "should not fork when arbiter is a child"


@mock.patch('os.fork')
def test_arbiter_calls_worker_exit(mock_os_fork):
    mock_os_fork.return_value = 0

    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.settings['worker_exit'] = mock.Mock()
    arbiter.pid = None
    mock_worker = mock.Mock()
    arbiter.worker_class = mock.Mock(return_value=mock_worker)
    try:
        arbiter.spawn_worker()
    except SystemExit:
        pass
    arbiter.cfg.worker_exit.assert_called_with(arbiter, mock_worker)


@mock.patch('os.waitpid')
def test_arbiter_reap_workers(mock_os_waitpid):
    mock_os_waitpid.side_effect = [(42, 0), (0, 0)]
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.settings['child_exit'] = mock.Mock()
    mock_worker = mock.Mock()
    arbiter.WORKERS = {42: mock_worker}
    arbiter.reap_workers()
    mock_worker.tmp.close.assert_called_with()
    arbiter.cfg.child_exit.assert_called_with(arbiter, mock_worker)


def test_arbiter_manage_companion_manager_spawns_when_configured():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.set("companion_workers", [{"name": "rq", "target": "pkg:run"}])
    arbiter.spawn_companion_manager = mock.Mock()
    arbiter.manage_companion_manager()
    arbiter.spawn_companion_manager.assert_called_once_with()


def test_arbiter_manage_companion_manager_noop_without_companions():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.spawn_companion_manager = mock.Mock()
    arbiter.manage_companion_manager()
    arbiter.spawn_companion_manager.assert_not_called()


def test_arbiter_manage_companion_manager_noop_when_running():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.set("companion_workers", [{"name": "rq", "target": "pkg:run"}])
    arbiter.companion_manager_pid = 4242
    arbiter.spawn_companion_manager = mock.Mock()
    arbiter.manage_companion_manager()
    arbiter.spawn_companion_manager.assert_not_called()


@mock.patch('os.waitpid')
def test_arbiter_reap_clears_companion_manager_pid(mock_os_waitpid):
    mock_os_waitpid.side_effect = [(4242, 0), (0, 0)]
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.companion_manager_pid = 4242
    arbiter.reap_workers()
    assert arbiter.companion_manager_pid == 0


def test_stop_companion_manager_signals_running():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.companion_manager_pid = 4242
    with mock.patch("os.kill") as kill:
        arbiter.stop_companion_manager(signal.SIGTERM)
    kill.assert_called_once_with(4242, signal.SIGTERM)


def test_stop_companion_manager_noop_when_not_running():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    with mock.patch("os.kill") as kill:
        arbiter.stop_companion_manager(signal.SIGTERM)
    kill.assert_not_called()


def test_stop_companion_manager_clears_pid_when_already_gone():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.companion_manager_pid = 4242
    with mock.patch("os.kill", side_effect=OSError(errno.ESRCH, "no such process")):
        arbiter.stop_companion_manager(signal.SIGTERM)
    assert arbiter.companion_manager_pid == 0


@mock.patch('os.waitpid')
def test_worker_reap_unaffected_by_companion_manager(mock_os_waitpid):
    # A worker exit is still reaped normally while a companion manager runs;
    # the companion reap branch must not swallow worker exits.
    mock_os_waitpid.side_effect = [(42, 0), (0, 0)]
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.settings['child_exit'] = mock.Mock()
    arbiter.companion_manager_pid = 9999
    mock_worker = mock.Mock()
    arbiter.WORKERS = {42: mock_worker}
    arbiter.reap_workers()
    mock_worker.tmp.close.assert_called_with()
    arbiter.cfg.child_exit.assert_called_with(arbiter, mock_worker)
    assert arbiter.companion_manager_pid == 9999


@mock.patch('os.fork', return_value=77)
def test_spawn_worker_unaffected_by_companions(mock_os_fork):
    # With companions configured, an HTTP worker is still spawned and recorded
    # exactly as before; companion config does not touch the worker path.
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.set("companion_workers", [{"name": "rq", "target": "pkg:run"}])
    arbiter.pid = 1234
    arbiter.WORKERS = {}  # instance dict, do not mutate the shared class attr
    mock_worker = mock.Mock()
    arbiter.worker_class = mock.Mock(return_value=mock_worker)
    pid = arbiter.spawn_worker()
    assert pid == 77
    assert arbiter.WORKERS[77] is mock_worker


def test_close_gunicorn_fds_in_manager_child():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    listener = mock.Mock()
    worker = mock.Mock()
    arbiter.LISTENERS = [listener]
    arbiter.WORKERS = {1: worker}
    arbiter.PIPE = [7, 8]
    with mock.patch("os.close") as os_close:
        arbiter._close_gunicorn_fds()
    listener.close.assert_called_once_with()
    worker.tmp.close.assert_called_once_with()
    assert os_close.call_count == 2


def test_reload_companion_manager_restarts_running():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.set("companion_workers", [{"name": "rq", "target": "pkg:run"}])
    arbiter.companion_manager_pid = 4242
    arbiter.stop_companion_manager = mock.Mock()
    arbiter.spawn_companion_manager = mock.Mock()
    arbiter.reload_companion_manager()
    arbiter.stop_companion_manager.assert_called_once_with(signal.SIGTERM)
    # pid still set (stop is mocked), so no respawn until the old one is reaped
    arbiter.spawn_companion_manager.assert_not_called()


def test_reload_companion_manager_starts_when_none_running():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.set("companion_workers", [{"name": "rq", "target": "pkg:run"}])
    arbiter.stop_companion_manager = mock.Mock()
    arbiter.spawn_companion_manager = mock.Mock()
    arbiter.reload_companion_manager()
    arbiter.stop_companion_manager.assert_not_called()
    arbiter.spawn_companion_manager.assert_called_once_with()


@mock.patch('gunicorn.sock.close_sockets')
def test_arbiter_stop_signals_companion_manager(close_sockets):
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.stop_companion_manager = mock.Mock()
    arbiter.stop()
    signals = [call.args[0] for call in arbiter.stop_companion_manager.call_args_list]
    assert signal.SIGTERM in signals
    assert signal.SIGKILL in signals


def test_companion_manager_stop_timeout_uses_explicit():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.set("companion_manager_stop_timeout", 120)
    assert arbiter.companion_manager_stop_timeout() == 120


def test_companion_manager_stop_timeout_derives_from_slowest():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    arbiter.cfg.set("companion_workers", [
        {"name": "rq", "target": "pkg:run", "stop_timeout": 300},
        {"name": "scheduler", "target": "pkg:sched", "stop_timeout": 30},
    ])
    arbiter.cfg.set("companion_manager_shutdown_buffer", 10)
    assert arbiter.companion_manager_stop_timeout() == 310


def test_companion_manager_stop_timeout_zero_without_companions():
    arbiter = gunicorn.arbiter.Arbiter(DummyApplication())
    assert arbiter.companion_manager_stop_timeout() == 0


class PreloadedAppWithEnvSettings(DummyApplication):
    """
    Simple application that makes use of the 'preload' feature to
    start the application before spawning worker processes and sets
    environmental variable configuration settings.
    """

    def load_config(self):
        """Set the 'preload_app' and 'raw_env' settings in order to verify their
        interaction below.
        """
        self.cfg.set('raw_env', [
            'SOME_PATH=/tmp/something', 'OTHER_PATH=/tmp/something/else'])
        self.cfg.set('preload_app', True)

    def wsgi(self):
        """Assert that the expected environmental variables are set when
        the main entry point of this application is called as part of a
        'preloaded' application.
        """
        verify_env_vars()
        return super().wsgi()


def verify_env_vars():
    assert os.getenv('SOME_PATH') == '/tmp/something'
    assert os.getenv('OTHER_PATH') == '/tmp/something/else'


def test_env_vars_available_during_preload():
    """Ensure that configured environmental variables are set during the
    initial set up of the application (called from the .setup() method of
    the Arbiter) such that they are available during the initial loading
    of the WSGI application.
    """
    # Note that we aren't making any assertions here, they are made in the
    # dummy application object being loaded here instead.
    gunicorn.arbiter.Arbiter(PreloadedAppWithEnvSettings())
