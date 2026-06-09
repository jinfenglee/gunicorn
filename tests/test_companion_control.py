#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import json
from unittest import mock

import pytest

from gunicorn.companion.control import (
    CommandError,
    ControlServer,
    decode_command,
    encode_response,
)


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
