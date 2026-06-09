#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

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
