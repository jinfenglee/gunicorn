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
    out = encode_response({"ok": True})
    assert out.endswith(b"\n")
    assert json.loads(out) == {"ok": True}


def test_handle_line_dispatches():
    server = ControlServer(dispatch=lambda obj: {"ok": True, "echo": obj["cmd"]},
                           path="/tmp/x.sock")
    out = server.handle_line('{"cmd": "status"}')
    assert json.loads(out) == {"ok": True, "echo": "status"}


def test_handle_line_bad_json_error_envelope():
    server = ControlServer(dispatch=lambda obj: {"ok": True}, path="/tmp/x.sock")
    out = json.loads(server.handle_line("garbage"))
    assert out["ok"] is False and "JSON" in out["error"]


def test_handle_line_dispatch_command_error():
    def dispatch(obj):
        raise CommandError("unknown command")
    server = ControlServer(dispatch=dispatch, path="/tmp/x.sock")
    out = json.loads(server.handle_line('{"cmd": "bogus"}'))
    assert out["ok"] is False and out["error"] == "unknown command"


def test_create_unlinks_stale_and_chmods():
    server = ControlServer(dispatch=lambda o: {}, path="/tmp/x.sock", mode=0o600)
    sock = mock.Mock()
    with mock.patch("os.path.exists", return_value=True), \
            mock.patch("os.unlink") as unlink, \
            mock.patch("socket.socket", return_value=sock), \
            mock.patch("os.chmod") as chmod:
        server.create()
    unlink.assert_called_once_with("/tmp/x.sock")
    sock.bind.assert_called_once_with("/tmp/x.sock")
    chmod.assert_called_once_with("/tmp/x.sock", 0o600)
    sock.listen.assert_called_once()


def test_close_unlinks():
    server = ControlServer(dispatch=lambda o: {}, path="/tmp/x.sock")
    server.sock = mock.Mock()
    with mock.patch("os.path.exists", return_value=True), \
            mock.patch("os.unlink") as unlink:
        server.close()
    unlink.assert_called_once_with("/tmp/x.sock")
    assert server.sock is None
