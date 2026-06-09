#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.


import json
import os
import socket


class CommandError(Exception):
    """A control request the manager understood but had to reject.

    Raised for malformed input (bad JSON, missing ``cmd``). It is turned into
    an ``{"ok": false, "error": ...}`` response rather than crashing the
    manager, so a buggy or hostile client can never take the socket down.
    """


def decode_command(line):
    """Parse one request line into a command dict.

    The wire protocol is newline-delimited JSON: each request is a single JSON
    object on its own line, e.g. ``{"cmd": "status"}``. Every request must be a
    JSON object carrying a string ``cmd``; anything else is a ``CommandError``.
    """
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        raise CommandError("invalid JSON")
    if not isinstance(obj, dict):
        raise CommandError("request must be a JSON object")
    if not isinstance(obj.get("cmd"), str):
        raise CommandError("missing 'cmd'")
    return obj


def encode_response(obj):
    """Encode a response dict as one newline-terminated JSON line of bytes."""
    return (json.dumps(obj) + "\n").encode("utf-8")


class ControlServer:
    """The manager's Unix-socket control endpoint.

    Owns the listening socket and the request framing only. Turning a decoded
    command into an action is delegated to ``dispatch`` (wired to the manager's
    command handlers in a later task); this class just decodes each line, runs
    it through ``dispatch``, and writes back the encoded reply.

    The socket is created with mode 0o600 and owned by the (non-root) user
    gunicorn runs as. There is no group-ownership switching.
    """

    def __init__(self, dispatch, path, mode=0o600, log=None, backlog=64):
        self.dispatch = dispatch
        self.path = path
        self.mode = mode
        self.log = log
        self.backlog = backlog
        self.sock = None

    def create(self):
        """Bind and listen on the Unix socket, replacing any stale one.

        A leftover socket file from a previous manager would make ``bind``
        fail, so it is unlinked first. Called once before the manager enters
        its run loop, as clients expect the socket to exist by then.
        """
        if os.path.exists(self.path):
            os.unlink(self.path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self.path)
        os.chmod(self.path, self.mode)
        sock.listen(self.backlog)
        self.sock = sock
        return sock

    def close(self):
        """Close the listening socket and remove its file."""
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if os.path.exists(self.path):
            os.unlink(self.path)

    def handle_line(self, line):
        """Run one request line and return the encoded response bytes.

        Both decoding and dispatch failures are caught and rendered as an
        error response, so one bad request never breaks the connection or the
        manager.
        """
        try:
            response = self.dispatch(decode_command(line))
        except CommandError as e:
            response = {"ok": False, "error": str(e)}
        return encode_response(response)

    def serve_connection(self, conn):
        """Serve newline-delimited requests on one accepted connection.

        Reads until the client hangs up, buffering partial reads and answering
        each complete line as it arrives. A trailing fragment without a newline
        is ignored.
        """
        buf = b""
        with conn:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        conn.sendall(self.handle_line(line))
