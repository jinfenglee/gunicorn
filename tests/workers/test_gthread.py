#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import socket
import types

from gunicorn.workers.gthread import ThreadWorker, REQUEST_LINE_PEEK


# _route_key is a staticmethod, so it can be exercised directly.

def test_route_key_basic():
    assert ThreadWorker._route_key(b"GET /index HTTP/1.1") == "GET /index"


def test_route_key_strips_query_string():
    assert ThreadWorker._route_key(
        b"GET /search?q=hello&p=2 HTTP/1.1") == "GET /search"


def test_route_key_post():
    assert ThreadWorker._route_key(
        b"POST /reports/generate HTTP/1.0") == "POST /reports/generate"


def test_route_key_malformed():
    assert ThreadWorker._route_key(b"") is None
    assert ThreadWorker._route_key(b"GARBAGE") is None
    assert ThreadWorker._route_key(None) is None


def _peek(conn):
    # _peek_request_line only touches conn.sock, so we can bind it to a stub
    return ThreadWorker._peek_request_line(object(), conn)


def test_peek_complete_request_line():
    a, b = socket.socketpair()
    try:
        a.setblocking(False)
        b.sendall(b"GET /x HTTP/1.1\r\nHost: y\r\n\r\n")
        conn = types.SimpleNamespace(sock=a)
        line, closed, complete = _peek(conn)
        assert line == b"GET /x HTTP/1.1"
        assert closed is False
        assert complete is True
        # MSG_PEEK must leave the bytes in the buffer for the parser
        assert a.recv(5) == b"GET /"
    finally:
        a.close()
        b.close()


def test_peek_incomplete_request_line_waits():
    a, b = socket.socketpair()
    try:
        a.setblocking(False)
        b.sendall(b"GET /x HTT")  # no CRLF yet
        conn = types.SimpleNamespace(sock=a)
        line, closed, complete = _peek(conn)
        assert line is None
        assert closed is False
        assert complete is False
    finally:
        a.close()
        b.close()


def test_peek_no_data_yet():
    a, b = socket.socketpair()
    try:
        a.setblocking(False)
        conn = types.SimpleNamespace(sock=a)
        line, closed, complete = _peek(conn)
        # nothing buffered: not closed, not complete -> keep waiting
        assert (line, closed, complete) == (None, False, False)
    finally:
        a.close()
        b.close()


def test_peek_peer_closed():
    a, b = socket.socketpair()
    a.setblocking(False)
    b.close()
    try:
        conn = types.SimpleNamespace(sock=a)
        line, closed, complete = _peek(conn)
        assert closed is True
    finally:
        a.close()


def test_peek_window_constant_is_reasonable():
    # a sanity bound so request lines fit comfortably in one peek
    assert REQUEST_LINE_PEEK >= 8192
