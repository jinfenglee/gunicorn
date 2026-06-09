#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import os
import signal
from unittest import mock

import pytest

from gunicorn.companion.control import CommandError
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


def test_status_lists_all_companions():
    mgr = make_manager("rq", "scheduler")
    entries = mgr.status(now=100.0)
    assert {e["name"] for e in entries} == {"rq", "scheduler"}
    assert all("state" in e and "description" in e for e in entries)


def test_handle_command_status():
    mgr = make_manager("rq")
    resp = mgr.handle_command({"cmd": "status"})
    assert resp["ok"] is True
    assert resp["companions"][0]["name"] == "rq"


def test_handle_command_start_routes():
    mgr = make_manager("rq")
    with mock.patch.object(mgr, "start_process",
                           return_value=(True, "rq started")) as sp:
        resp = mgr.handle_command({"cmd": "start", "name": "rq"})
    sp.assert_called_once_with("rq")
    assert resp == {"ok": True, "message": "rq started"}


def test_handle_command_stop_and_restart_route():
    mgr = make_manager("rq")
    with mock.patch.object(mgr, "stop_process", return_value=(True, "s")) as st, \
            mock.patch.object(mgr, "restart_process", return_value=(True, "r")) as rt:
        mgr.handle_command({"cmd": "stop", "name": "rq"})
        mgr.handle_command({"cmd": "restart", "name": "rq"})
    st.assert_called_once_with("rq")
    rt.assert_called_once_with("rq")


def test_handle_command_missing_name():
    mgr = make_manager("rq")
    with pytest.raises(CommandError):
        mgr.handle_command({"cmd": "start"})


def test_handle_command_unknown():
    mgr = make_manager("rq")
    with pytest.raises(CommandError):
        mgr.handle_command({"cmd": "reread"})


def cfg(name, **kw):
    return CompanionConfig(name=name, target=lambda: None, **kw)


def test_reread_adds_new():
    mgr = make_manager("rq")
    new = [cfg("rq"), cfg("scheduler")]
    with mock.patch("os.fork", return_value=10):
        result = mgr.reread_config(new)
    assert result["added"] == ["scheduler"]
    assert "scheduler" in mgr.processes
    assert mgr.processes["scheduler"].state == State.STARTING


def test_reread_removes_missing():
    mgr = make_manager("rq", "scheduler")
    mgr.processes["scheduler"].state = State.RUNNING
    mgr.processes["scheduler"].pid = 11
    with mock.patch("os.kill"):
        result = mgr.reread_config([cfg("rq")])
    assert result["removed"] == ["scheduler"]
    assert "scheduler" not in mgr.processes


def test_reread_restarts_changed():
    mgr = make_manager("rq")
    mgr.processes["rq"].state = State.RUNNING
    mgr.processes["rq"].pid = 12
    changed = cfg("rq", env={"X": "1"})  # different hash
    with mock.patch("os.kill"):
        result = mgr.reread_config([changed])
    assert result["restarted"] == ["rq"]
    assert mgr.processes["rq"].config is changed
    assert mgr.processes["rq"].state == State.STOPPING


def test_reread_changed_manual_stop_keeps_stopped():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.manual_stop = True
    proc.state = State.STOPPED
    changed = cfg("rq", env={"X": "1"})
    result = mgr.reread_config([changed])
    assert result["unchanged"] == ["rq"]
    assert proc.config is changed and proc.state == State.STOPPED


def test_reread_unchanged_noop():
    mgr = make_manager("rq")
    same = mgr.processes["rq"].config
    result = mgr.reread_config([same])
    assert result["unchanged"] == ["rq"]
    assert result["restarted"] == []


def test_reread_duplicate_name_keeps_old():
    mgr = make_manager("rq")
    result = mgr.reread_config([cfg("rq"), cfg("rq")])
    assert result["ok"] is False and result["kept_old_config"] is True
    assert "duplicate" in result["error"]


def test_handle_command_reread_no_loader():
    mgr = make_manager("rq")
    with pytest.raises(CommandError):
        mgr.handle_command({"cmd": "reread"})


def test_handle_command_reread_runs_loader():
    mgr = make_manager("rq")
    mgr.config_loader = lambda: [mgr.processes["rq"].config]
    resp = mgr.handle_command({"cmd": "reread"})
    assert resp["ok"] is True and resp["unchanged"] == ["rq"]


def test_handle_command_reread_bad_config():
    mgr = make_manager("rq")
    def boom():
        raise ValueError("duplicate companion name rq")
    mgr.config_loader = boom
    resp = mgr.handle_command({"cmd": "reread"})
    assert resp["ok"] is False and resp["kept_old_config"] is True


def test_start_process_stopped_spawns():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    with mock.patch("os.fork", return_value=70) as fork:
        ok, _ = mgr.start_process("rq")
    fork.assert_called_once()
    assert ok and proc.state == State.STARTING and proc.manual_stop is False


def test_start_process_backoff_cancels_retry():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.BACKOFF
    proc.next_retry_at = 999.0
    proc.manual_stop = True
    with mock.patch("os.fork", return_value=71):
        ok, _ = mgr.start_process("rq")
    assert ok and proc.state == State.STARTING
    assert proc.next_retry_at is None and proc.manual_stop is False


def test_start_process_running_is_noop():
    mgr = make_manager("rq")
    mgr.processes["rq"].state = State.RUNNING
    with mock.patch("os.fork") as fork:
        ok, _ = mgr.start_process("rq")
    assert ok
    fork.assert_not_called()


def test_start_process_stopping_rejected():
    mgr = make_manager("rq")
    mgr.processes["rq"].state = State.STOPPING
    ok, msg = mgr.start_process("rq")
    assert not ok and "stopping" in msg


def test_start_process_unknown():
    mgr = make_manager("rq")
    ok, _ = mgr.start_process("nope")
    assert not ok


def test_stop_process_running_signals_and_stopping():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.RUNNING
    proc.pid = 80
    proc.config.stop_timeout = 60
    with mock.patch("os.kill") as kill:
        ok, _ = mgr.stop_process("rq", now=200.0)
    kill.assert_called_once_with(80, signal.SIGTERM)
    assert ok and proc.state == State.STOPPING
    assert proc.manual_stop is True and proc.stop_deadline == 260.0


def test_stop_process_backoff_to_stopped():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.BACKOFF
    proc.next_retry_at = 999.0
    with mock.patch("os.kill") as kill:
        ok, _ = mgr.stop_process("rq")
    kill.assert_not_called()
    assert ok and proc.state == State.STOPPED
    assert proc.next_retry_at is None and proc.manual_stop is True


def test_stop_process_already_stopped():
    mgr = make_manager("rq")
    with mock.patch("os.kill") as kill:
        ok, _ = mgr.stop_process("rq")
    kill.assert_not_called()
    assert ok and mgr.processes["rq"].manual_stop is True


def test_stop_process_unknown():
    mgr = make_manager("rq")
    ok, _ = mgr.stop_process("nope")
    assert not ok


def test_signal_number_resolves_name():
    assert CompanionManager._signal_number("SIGKILL") == signal.SIGKILL
    assert CompanionManager._signal_number(9) == 9


def test_signal_number_rejects_bad():
    with pytest.raises(ValueError):
        CompanionManager._signal_number("SIGTRM")


def test_restart_process_running_stops_with_reload_timeout():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.RUNNING
    proc.pid = 90
    proc.config.reload_timeout = 30
    proc.manual_stop = True
    with mock.patch("os.kill") as kill:
        ok, _ = mgr.restart_process("rq", now=300.0)
    kill.assert_called_once_with(90, signal.SIGTERM)
    assert ok and proc.state == State.STOPPING
    assert proc.restart_pending is True and proc.stop_deadline == 330.0
    assert proc.manual_stop is False


def test_restart_pending_reap_respawns_immediately():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.STOPPING
    proc.restart_pending = True
    proc.pid = 91
    with mock.patch("os.waitpid", side_effect=[(91, 0), (0, 0)]), \
            mock.patch("os.fork", return_value=92):
        mgr.reap_processes()
    assert proc.state == State.STARTING
    assert proc.pid == 92
    assert proc.restart_pending is False
    assert proc.restart_count == 1


def test_restart_process_stopped_starts_now():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    with mock.patch("os.fork", return_value=93), mock.patch("os.kill") as kill:
        ok, _ = mgr.restart_process("rq")
    kill.assert_not_called()
    assert ok and proc.state == State.STARTING


def test_restart_process_backoff_starts_now():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.BACKOFF
    proc.next_retry_at = 999.0
    with mock.patch("os.fork", return_value=94):
        ok, _ = mgr.restart_process("rq")
    assert ok and proc.state == State.STARTING and proc.next_retry_at is None


def test_restart_process_stopping_rejected():
    mgr = make_manager("rq")
    mgr.processes["rq"].state = State.STOPPING
    ok, msg = mgr.restart_process("rq")
    assert not ok and "stopping" in msg


def test_manual_stop_preserved_through_exit():
    # stop a running companion, then reap its child: it must settle in STOPPED
    # with manual_stop still set so it is not auto-restarted.
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.RUNNING
    proc.pid = 60
    with mock.patch("os.kill"):
        mgr.stop_process("rq", now=10.0)
    with mock.patch("os.waitpid", side_effect=[(60, 0), (0, 0)]), \
            mock.patch("os.fork") as fork:
        mgr.reap_processes()
    fork.assert_not_called()
    assert proc.state == State.STOPPED and proc.manual_stop is True


def test_start_clears_manual_stop():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.manual_stop = True
    with mock.patch("os.fork", return_value=61):
        mgr.start_process("rq")
    assert proc.manual_stop is False


def test_spawn_does_not_touch_manual_stop():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.manual_stop = True
    with mock.patch("os.fork", return_value=62):
        mgr.spawn_process(proc)
    assert proc.manual_stop is True


def test_handle_exit_unexpected_backoff():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.restart_delay = 5
    mgr.handle_exit(proc, now=100.0)
    assert proc.state == State.BACKOFF
    assert proc.next_retry_at == 105.0


def test_handle_exit_manual_stop_stays_stopped():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.manual_stop = True
    mgr.handle_exit(proc, now=100.0)
    assert proc.state == State.STOPPED
    assert proc.next_retry_at is None


def test_retry_backoff_respawns_when_due():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.BACKOFF
    proc.next_retry_at = 100.0
    with mock.patch("os.fork", return_value=555):
        retried = mgr.retry_backoff(now=101.0)
    assert retried == [proc]
    assert proc.restart_count == 1
    assert proc.state == State.STARTING
    assert proc.pid == 555


def test_retry_backoff_waits_until_due():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.BACKOFF
    proc.next_retry_at = 100.0
    assert mgr.retry_backoff(now=99.0) == []
    assert proc.state == State.BACKOFF


def test_reap_unexpected_exit_enters_backoff():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.pid = 4321
    with mock.patch("os.waitpid", side_effect=[(4321, 1 << 8), (0, 0)]):
        mgr.reap_processes()
    assert proc.state == State.BACKOFF
    assert proc.next_retry_at is not None


def test_promote_running_after_startsecs():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.config.startsecs = 1
    proc.state = State.STARTING
    proc.started_at = 100.0
    promoted = mgr.promote_running(now=101.5)
    assert promoted == [proc]
    assert proc.state == State.RUNNING


def test_promote_running_too_early():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.config.startsecs = 5
    proc.state = State.STARTING
    proc.started_at = 100.0
    assert mgr.promote_running(now=102.0) == []
    assert proc.state == State.STARTING


def test_promote_running_ignores_non_starting():
    mgr = make_manager("rq")
    proc = mgr.processes["rq"]
    proc.state = State.BACKOFF
    proc.started_at = 100.0
    assert mgr.promote_running(now=999.0) == []
    assert proc.state == State.BACKOFF


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
