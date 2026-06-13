#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

from unittest import mock

import pytest

from gunicorn.companion import ctl


def test_run_status_prints_and_returns_zero(capsys):
    with mock.patch.object(
        ctl, "send_command", return_value={"ok": True, "companions": []}
    ) as send:
        code = ctl.run(["--socket", "/tmp/x.sock", "status"])
    assert code == 0
    send.assert_called_once_with("/tmp/x.sock", {"cmd": "status"})
    assert "ok" in capsys.readouterr().out


def test_run_per_name_command_sends_name():
    with mock.patch.object(
        ctl, "send_command", return_value={"ok": True, "message": "x"}
    ) as send:
        code = ctl.run(["--socket", "/tmp/x.sock", "stop", "ticker"])
    assert code == 0
    send.assert_called_once_with("/tmp/x.sock", {"cmd": "stop", "name": "ticker"})


def test_run_failure_response_returns_one():
    with mock.patch.object(
        ctl, "send_command", return_value={"ok": False, "error": "bad"}
    ):
        assert ctl.run(["--socket", "/tmp/x.sock", "status"]) == 1


def test_run_per_name_command_requires_name():
    with pytest.raises(SystemExit):
        ctl.run(["--socket", "/tmp/x.sock", "stop"])


def test_run_requires_socket(monkeypatch):
    monkeypatch.delenv("GUNICORN_COMPANION_SOCKET", raising=False)
    with pytest.raises(SystemExit):
        ctl.run(["status"])


def test_run_unreachable_socket_returns_two():
    with mock.patch.object(ctl, "send_command", side_effect=OSError("nope")):
        assert ctl.run(["--socket", "/tmp/x.sock", "status"]) == 2


def test_send_command_round_trip():
    client = mock.Mock()
    client.recv.side_effect = [b'{"ok": true}\n']
    with mock.patch("socket.socket", return_value=client):
        result = ctl.send_command("/tmp/x.sock", {"cmd": "status"})
    client.connect.assert_called_once_with("/tmp/x.sock")
    assert client.sendall.call_args.args[0] == b'{"cmd": "status"}\n'
    assert result == {"ok": True}
