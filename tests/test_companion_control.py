#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import json
from unittest import mock

import pytest

from gunicorn.companion.config import CompanionConfig
from gunicorn.companion.control import (
    CommandError,
    ControlServer,
    decode_command,
    encode_response,
)
from gunicorn.companion.manager import CompanionManager


def make_manager(*names):
    configs = [CompanionConfig(name=name, target=lambda: None) for name in names]
    return CompanionManager(configs, log=mock.Mock())


def server_for(manager):
    return ControlServer(dispatch=manager.handle_command, path="/tmp/x.sock")


def test_decode_command_valid():
    assert decode_command('{"cmd": "status"}') == {"cmd": "status"}


def test_decode_command_bad_json():
    with pytest.raises(CommandError):
        decode_command("{not json")


def test_decode_command_not_object():
    with pytest.raises(CommandError):
        decode_command("[1, 2, 3]")


def test_decode_command_missing_cmd():
    with pytest.raises(CommandError):
        decode_command('{"name": "rq"}')


def test_encode_response_newline_terminated():
    response = encode_response({"ok": True})
    assert response.endswith(b"\n")
    assert json.loads(response) == {"ok": True}


def test_handle_line_dispatches():
    server = ControlServer(
        dispatch=lambda command: {"ok": True, "echo": command["cmd"]},
        path="/tmp/x.sock")
    response = server.handle_line('{"cmd": "status"}')
    assert json.loads(response) == {"ok": True, "echo": "status"}


def test_handle_line_bad_json_error_envelope():
    server = ControlServer(dispatch=lambda command: {"ok": True}, path="/tmp/x.sock")
    response = json.loads(server.handle_line("garbage"))
    assert response["ok"] is False and "JSON" in response["error"]


def test_handle_line_dispatch_command_error():
    def dispatch(command):
        raise CommandError("unknown command")
    server = ControlServer(dispatch=dispatch, path="/tmp/x.sock")
    response = json.loads(server.handle_line('{"cmd": "bogus"}'))
    assert response["ok"] is False and response["error"] == "unknown command"


def test_handle_line_unexpected_exception_caught():
    def dispatch(command):
        raise ValueError("unknown stop signal 'SIGTRM'")
    server = ControlServer(dispatch=dispatch, path="/tmp/x.sock",
                           log=mock.Mock())
    response = json.loads(server.handle_line('{"cmd": "stop", "name": "rq"}'))
    assert response["ok"] is False and "internal error" in response["error"]


def test_create_unlinks_stale_and_chmods():
    server = ControlServer(dispatch=lambda command: {}, path="/tmp/x.sock",
                           mode=0o600)
    listener = mock.Mock()
    with mock.patch("os.path.exists", return_value=True), \
            mock.patch("os.unlink") as unlink, \
            mock.patch("socket.socket", return_value=listener), \
            mock.patch("os.chmod") as chmod:
        server.create()
    unlink.assert_called_once_with("/tmp/x.sock")
    listener.bind.assert_called_once_with("/tmp/x.sock")
    chmod.assert_called_once_with("/tmp/x.sock", 0o600)
    listener.listen.assert_called_once()


def test_close_unlinks():
    server = ControlServer(dispatch=lambda command: {}, path="/tmp/x.sock")
    server.listener = mock.Mock()
    with mock.patch("os.path.exists", return_value=True), \
            mock.patch("os.unlink") as unlink:
        server.close()
    unlink.assert_called_once_with("/tmp/x.sock")
    assert server.listener is None


def test_control_status_command_end_to_end():
    manager = make_manager("rq")
    response = json.loads(server_for(manager).handle_line('{"cmd": "status"}'))
    assert response["ok"] is True
    assert response["companions"][0]["name"] == "rq"


def test_control_start_command_end_to_end():
    manager = make_manager("rq")
    with mock.patch("os.fork", return_value=10):
        response = json.loads(
            server_for(manager).handle_line('{"cmd": "start", "name": "rq"}'))
    assert response["ok"] is True
    assert "rq" in response["message"]


def test_control_unknown_command_error_envelope():
    manager = make_manager("rq")
    response = json.loads(
        server_for(manager).handle_line('{"cmd": "bogus", "name": "rq"}'))
    assert response["ok"] is False
    assert "unknown" in response["error"]


def test_control_missing_name_error_envelope():
    manager = make_manager("rq")
    response = json.loads(server_for(manager).handle_line('{"cmd": "start"}'))
    assert response["ok"] is False
    assert "name" in response["error"]


def test_control_reread_without_loader_error_envelope():
    manager = make_manager("rq")
    response = json.loads(server_for(manager).handle_line('{"cmd": "reread"}'))
    assert response["ok"] is False
    assert "reread" in response["error"]
