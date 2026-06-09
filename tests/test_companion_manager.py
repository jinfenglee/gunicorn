#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import os
from unittest import mock

import pytest

from gunicorn.companion.manager import CompanionManager
from gunicorn.companion.process import CompanionConfig, State


def make_manager(*names):
    configs = [CompanionConfig(name=n, target=lambda: None) for n in names]
    return CompanionManager(configs, log=mock.Mock())


def test_manager_builds_one_process_per_config():
    mgr = make_manager("rq", "scheduler")
    assert set(mgr.processes) == {"rq", "scheduler"}
    assert mgr.processes["rq"].state == State.STOPPED


def test_resolve_target_accepts_callable():
    fn = lambda: None
    assert CompanionManager._resolve_target(fn) is fn


def test_resolve_target_import_string():
    # os.getpid is a real "module:attr" target.
    assert CompanionManager._resolve_target("os:getpid") is __import__("os").getpid


def test_resolve_target_rejects_bad_string():
    with pytest.raises(ValueError):
        CompanionManager._resolve_target("no_colon")


def test_apply_environment_sets_cwd_and_env():
    config = CompanionConfig(name="rq", target=lambda: None,
                             cwd="/tmp", env={"COMPANION_X": "1"})
    with mock.patch("os.chdir") as chdir, \
            mock.patch.dict("os.environ", {}, clear=False):
        CompanionManager._apply_environment(config)
        chdir.assert_called_once_with("/tmp")
        import os
        assert os.environ["COMPANION_X"] == "1"


def test_apply_environment_noop_without_cwd_env():
    config = CompanionConfig(name="rq", target=lambda: None)
    with mock.patch("os.chdir") as chdir:
        CompanionManager._apply_environment(config)
        chdir.assert_not_called()


def test_open_output_inherit_returns_none():
    assert CompanionManager._open_output(None) is None
    assert CompanionManager._open_output("inherit") is None


def test_open_output_path_opens_append():
    with mock.patch("os.open", return_value=9) as op:
        fd = CompanionManager._open_output("/var/log/rq.log")
    assert fd == 9
    flags = op.call_args.args[1]
    assert flags & os.O_APPEND and flags & os.O_CREAT


def test_redirect_output_files():
    config = CompanionConfig(name="rq", target=lambda: None,
                             stdout="/o.log", stderr="/e.log")
    with mock.patch("os.open", side_effect=[10, 11]), \
            mock.patch("os.dup2") as dup2:
        CompanionManager._redirect_output(config)
    dup2.assert_any_call(10, 1)
    dup2.assert_any_call(11, 2)


def test_redirect_output_stderr_to_stdout():
    config = CompanionConfig(name="rq", target=lambda: None,
                             stdout="/o.log", stderr="stdout")
    with mock.patch("os.open", return_value=10), \
            mock.patch("os.dup2") as dup2:
        CompanionManager._redirect_output(config)
    dup2.assert_any_call(10, 1)
    dup2.assert_any_call(1, 2)


def test_redirect_output_inherit_noop():
    config = CompanionConfig(name="rq", target=lambda: None)
    with mock.patch("os.dup2") as dup2:
        CompanionManager._redirect_output(config)
    dup2.assert_not_called()


def test_reap_records_exit_code():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.pid = 4321
    # exit code 1 -> status 1<<8; second call drains the queue.
    with mock.patch("os.waitpid", side_effect=[(4321, 1 << 8), (0, 0)]):
        reaped = mgr.reap_processes()
    assert reaped == [proc]
    assert proc.last_exit_code == 1
    assert proc.last_exit_signal is None
    assert proc.exit_count == 1
    assert proc.pid is None


def test_reap_records_signal():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.pid = 4321
    with mock.patch("os.waitpid", side_effect=[(4321, 9), (0, 0)]):
        mgr.reap_processes()
    assert proc.last_exit_signal == 9
    assert proc.last_exit_code is None


def test_reap_no_children():
    mgr = make_manager("rq")
    with mock.patch("os.waitpid", side_effect=ChildProcessError):
        assert mgr.reap_processes() == []


def test_spawn_parent_records_pid_and_starting():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    with mock.patch("os.fork", return_value=4321):
        pid = mgr.spawn_process(proc)
    assert pid == 4321
    assert proc.pid == 4321
    assert proc.state == State.STARTING
    assert proc.started_at is not None
    assert proc.manual_stop is False
