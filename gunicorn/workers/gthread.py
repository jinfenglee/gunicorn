#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

# design:
# A threaded worker accepts connections in the main loop, accepted
# connections are added to the thread pool as a connection job.
# Keepalive connections are put back in the loop waiting for an event.
# If no event happen after the keep alive timeout, the connection is
# closed.
# pylint: disable=no-else-break

from concurrent import futures
import errno
import faulthandler
import os
import selectors
import socket
import ssl
import sys
import time
from collections import deque
from datetime import datetime
from functools import partial
from threading import RLock

from . import base
from .gthread_routing import SlowRoutePredictor
from .. import http
from .. import util
from .. import sock
from ..http import wsgi

# how many bytes to peek when classifying a request by its request line
REQUEST_LINE_PEEK = 8192


class TConn:

    def __init__(self, cfg, sock, client, server):
        self.cfg = cfg
        self.sock = sock
        self.client = client
        self.server = server

        self.timeout = None
        self.parser = None
        # route key (method + path), set by the worker when request routing is
        # enabled; used to predict and learn slow routes
        self.route_key = None

        # set the socket to non blocking
        self.sock.setblocking(False)

    def init(self):
        self.sock.setblocking(True)
        if self.parser is None:
            # wrap the socket if needed
            if self.cfg.is_ssl:
                self.sock = sock.ssl_wrap_socket(self.sock, self.cfg)

            # initialize the parser
            self.parser = http.RequestParser(self.cfg, self.sock, self.client)

    def set_timeout(self):
        # set the timeout
        self.timeout = time.time() + self.cfg.keepalive

    def close(self):
        util.close(self.sock)


class ThreadWorker(base.Worker):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.worker_connections = self.cfg.worker_connections
        self.max_keepalived = self.cfg.worker_connections - self.cfg.threads
        # initialise the pool(s): a single pool when routing is disabled, or a
        # separate fast (``self.tpool``) and slow pool when it is enabled
        self.tpool = None
        self.slow_pool = None
        # number of slow requests submitted but not yet finished, used to bound
        # the slow lane (running + queued) and shed load with 503
        self.nr_slow = 0
        self.poller = None
        self.shutdown_event = os.eventfd(0)
        self._lock = None
        self.futures = deque()
        self._keep = deque()
        self.nr_conns = 0

        # request routing: when a slow-request threshold is configured, slow
        # requests are routed to a separate lane so they cannot starve fast ones
        self.slow_threshold = self.cfg.slow_request_threshold
        self.routing_enabled = self.slow_threshold > 0
        self.predictor = None

    @classmethod
    def check_config(cls, cfg, log):
        max_keepalived = cfg.worker_connections - cfg.threads

        if max_keepalived <= 0 and cfg.keepalive:
            log.warning("No keepalived connections can be handled. " +
                        "Check the number of worker connections and threads.")

    def init_process(self):
        self.tpool = self.get_thread_pool()
        if self.routing_enabled:
            self.predictor = SlowRoutePredictor(self.slow_threshold)
            # a dedicated pool for the slow lane: slow requests can never
            # occupy the fast pool's (``self.tpool``) threads
            self.slow_pool = futures.ThreadPoolExecutor(
                max_workers=self.cfg.slow_threads)
        self.poller = selectors.DefaultSelector()
        self._lock = RLock()
        super().init_process()

    def get_thread_pool(self):
        """Override this method to customize how the thread pool is created"""
        return futures.ThreadPoolExecutor(max_workers=self.cfg.threads)

    def _shutdown_pools(self, wait):
        for pool in (self.tpool, self.slow_pool):
            if pool is not None:
                pool.shutdown(wait)

    def handle_exit(self, sig, frame):
        self.alive = False
        os.eventfd_write(self.shutdown_event, 1)

    def handle_quit(self, sig, frame):
        self.alive = False
        # worker_int callback
        self.cfg.worker_int(self)
        self._shutdown_pools(False)
        time.sleep(0.1)
        sys.exit(0)

    def _wrap_future(self, fs, conn, slow=False):
        fs.conn = conn
        fs.slow = slow
        fs._start_time = time.monotonic()
        fs._request_timeout = fs._start_time + self.cfg.timeout
        fs._observed_slow = False
        self.futures.append(fs)
        fs.add_done_callback(self.finish_request)

    def enqueue_req(self, conn, slow=False):
        conn.init()
        # submit the connection to the appropriate pool
        if self.routing_enabled and slow:
            cap = self.cfg.slow_queue_maxsize
            if cap and self.nr_slow >= self.cfg.slow_threads + cap:
                # slow lane (running + queued) is full; shed load with 503
                # instead of letting the slow queue grow unbounded
                self.reject_overloaded(conn)
                return
            self.nr_slow += 1
            fs = self.slow_pool.submit(self.handle, conn)
        else:
            fs = self.tpool.submit(self.handle, conn)
        self._wrap_future(fs, conn, slow=slow)

    def accept(self, server, listener):
        try:
            sock, client = listener.accept()
            # initialize the connection object
            conn = TConn(self.cfg, sock, client, server)

            self.nr_conns += 1
            if self.routing_enabled and not self.cfg.is_ssl:
                # park the connection until its request line is readable, then
                # classify and route it to the fast or slow lane
                self.park_for_request(conn)
            else:
                # legacy single-lane path (also used for SSL, whose request
                # line cannot be peeked before the TLS handshake)
                self.enqueue_req(conn)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.ECONNABORTED,
                               errno.EWOULDBLOCK):
                raise

    def park_for_request(self, conn):
        """Register a connection in the poller until its request line arrives."""
        conn.sock.setblocking(False)
        conn.set_timeout()
        with self._lock:
            self._keep.append(conn)
            self.poller.register(conn.sock, selectors.EVENT_READ,
                                 partial(self.classify_and_dispatch, conn))

    def classify_and_dispatch(self, conn, client=None):
        """Peek the request line, predict the lane, and enqueue the request."""
        line, closed, complete = self._peek_request_line(conn)
        if not closed and not complete:
            # request line has not fully arrived yet; keep waiting. Stalled
            # clients are reaped by murder_keepalived via the connection timeout.
            return

        with self._lock:
            try:
                # remove the connection from the parked set
                self._keep.remove(conn)
            except ValueError:
                # already handled (e.g. by murder_keepalived); nothing to do
                return
            try:
                self.poller.unregister(conn.sock)
            except (KeyError, OSError, ValueError):
                pass

        if closed:
            self.nr_conns -= 1
            conn.close()
            return

        conn.route_key = self._route_key(line)
        slow = self.predictor.is_slow(conn.route_key)
        self.enqueue_req(conn, slow=slow)

    def _peek_request_line(self, conn):
        """Return ``(line, closed, complete)`` for the connection's request line.

        ``line`` is the request line bytes (without CRLF) once available,
        ``closed`` is True if the peer closed the connection, and ``complete``
        is True once we should stop waiting for more data.
        """
        try:
            data = conn.sock.recv(REQUEST_LINE_PEEK, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            return None, False, False
        except OSError:
            return None, True, False

        if data == b"":
            # peer closed the connection before sending a request
            return None, True, False

        idx = data.find(b"\r\n")
        if idx == -1:
            if len(data) >= REQUEST_LINE_PEEK:
                # request line longer than our peek window; stop classifying and
                # let the worker's parser deal with (or reject) it
                return None, False, True
            return None, False, False
        return data[:idx], False, True

    @staticmethod
    def _route_key(line):
        """Build a route key (``"METHOD /path"``) from a raw request line."""
        if not line:
            return None
        parts = line.split(b" ")
        if len(parts) < 2:
            return None
        try:
            method = parts[0].decode("latin1")
            path = parts[1].split(b"?", 1)[0].decode("latin1")
        except UnicodeDecodeError:
            return None
        return method + " " + path

    def reject_overloaded(self, conn):
        """Reject a connection with 503 because the slow lane is saturated."""
        self.nr_conns -= 1
        try:
            conn.sock.setblocking(True)
            conn.sock.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 0\r\n"
                b"Retry-After: %d\r\n\r\n"
                % int(self.cfg.slow_lane_retry_after))
        except OSError:
            pass
        finally:
            conn.close()

    def reuse_connection(self, conn, client):
        with self._lock:
            # unregister the client from the poller
            self.poller.unregister(client)
            # remove the connection from keepalive
            try:
                self._keep.remove(conn)
            except ValueError:
                # race condition
                return

        # submit the connection to a worker
        self.enqueue_req(conn)

    def on_shutdown_event(self, *args):
        # Drain any readable input to avoid getting polled again
        _ = os.eventfd_read(self.shutdown_event)

    def murder_keepalived(self):
        now = time.time()
        while True:
            with self._lock:
                try:
                    # remove the connection from the queue
                    conn = self._keep.popleft()
                except IndexError:
                    break

            delta = conn.timeout - now
            if delta > 0:
                # add the connection back to the queue
                with self._lock:
                    self._keep.appendleft(conn)
                break
            else:
                self.nr_conns -= 1
                # remove the socket from the poller
                with self._lock:
                    try:
                        self.poller.unregister(conn.sock)
                    except OSError as e:
                        if e.errno != errno.EBADF:
                            raise
                    except KeyError:
                        # already removed by the system, continue
                        pass
                    except ValueError:
                        # already removed by the system continue
                        pass

                # close the socket
                conn.close()

    def is_parent_alive(self):
        # If our parent changed then we shut down.
        if self.ppid != os.getppid():
            self.log.info("Parent changed, shutting down: %s", self)
            return False
        return True

    def run(self):
        # init listeners, add them to the event loop
        for sock in self.sockets:
            sock.setblocking(False)
            # a race condition during graceful shutdown may make the listener
            # name unavailable in the request handler so capture it once here
            server = sock.getsockname()
            acceptor = partial(self.accept, server)
            self.poller.register(sock, selectors.EVENT_READ, acceptor)

        # This is just used to wake up the poller, nothing else needs to be done.
        self.poller.register(self.shutdown_event, selectors.EVENT_READ, self.on_shutdown_event)

        while self.alive:
            # notify the arbiter we are alive
            self.notify()

            # can we accept more connections?
            if self.nr_conns < self.worker_connections:
                # wait for an event
                select_timeout = self.timeout or 1.0
                if self._keep:
                    select_timeout = min(select_timeout, self.cfg.keepalive)
                events = self.poller.select(select_timeout)
                for key, _ in events:
                    callback = key.data
                    callback(key.fileobj)

                # check (but do not wait) for finished requests
                result = futures.wait(self.futures, timeout=0,
                                      return_when=futures.FIRST_COMPLETED)
            else:
                # wait for a request to finish
                result = futures.wait(self.futures, timeout=1.0,
                                      return_when=futures.FIRST_COMPLETED)

            # clean up finished requests
            for fut in result.done:
                self.futures.remove(fut)

            if not self.is_parent_alive():
                break

            # handle keepalive timeouts
            self.murder_keepalived()

            # `gthread` does not implement ANY kind of request timeout, the
            # simplest request timeout will kill the entire worker.
            current_time = time.monotonic()
            for fut in self.futures:
                if current_time > fut._request_timeout:
                    self.alive = False
                    self.log.error("A request timed out. Exiting.")
                    faulthandler.dump_traceback()
                elif (self.routing_enabled and not fut._observed_slow
                        and not fut.slow
                        and current_time - fut._start_time > self.slow_threshold):
                    # an in-flight fast-lane request crossed the threshold; learn
                    # the route as slow now so the rest of a burst is rerouted
                    # without waiting for this request to finish
                    self.predictor.observe_slow(fut.conn.route_key)
                    fut._observed_slow = True

        self._shutdown_pools(False)
        self.poller.close()

        for s in self.sockets:
            s.close()

        futures.wait(self.futures, timeout=self.cfg.graceful_timeout)

    def finish_request(self, fs):
        # the slow request is done (whatever the outcome): free its slow slot
        if self.routing_enabled and fs.slow:
            self.nr_slow -= 1

        if fs.cancelled():
            self.nr_conns -= 1
            fs.conn.close()
            return

        # feed the observed processing time back to the predictor so the route
        # is learned (or unlearned) as slow
        if self.routing_enabled and fs.conn.route_key:
            self.predictor.update(fs.conn.route_key,
                                  time.monotonic() - fs._start_time)

        try:
            (keepalive, conn) = fs.result()
            # if the connection should be kept alived add it
            # to the eventloop and record it
            if keepalive and self.alive:
                if self.routing_enabled and not self.cfg.is_ssl:
                    # re-classify the next request on this connection
                    self.park_for_request(conn)
                else:
                    # flag the socket as non blocked
                    conn.sock.setblocking(False)

                    # register the connection
                    conn.set_timeout()
                    with self._lock:
                        self._keep.append(conn)

                        # add the socket to the event loop
                        self.poller.register(conn.sock, selectors.EVENT_READ,
                                             partial(self.reuse_connection, conn))
            else:
                self.nr_conns -= 1
                conn.close()
        except Exception:
            # an exception happened, make sure to close the
            # socket.
            self.nr_conns -= 1
            fs.conn.close()

    def handle(self, conn):
        keepalive = False
        req = None
        try:
            req = next(conn.parser)
            if not req:
                return (False, conn)

            # handle the request
            keepalive = self.handle_request(req, conn)
            if keepalive:
                return (keepalive, conn)
        except http.errors.NoMoreData as e:
            self.log.debug("Ignored premature client disconnection. %s", e)

        except StopIteration as e:
            self.log.debug("Closing connection. %s", e)
        except ssl.SSLError as e:
            if e.args[0] == ssl.SSL_ERROR_EOF:
                self.log.debug("ssl connection closed")
                conn.sock.close()
            else:
                self.log.debug("Error processing SSL request.")
                self.handle_error(req, conn.sock, conn.client, e)

        except OSError as e:
            if e.errno not in (errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN):
                self.log.exception("Socket error processing request.")
            else:
                if e.errno == errno.ECONNRESET:
                    self.log.debug("Ignoring connection reset")
                elif e.errno == errno.ENOTCONN:
                    self.log.debug("Ignoring socket not connected")
                else:
                    self.log.debug("Ignoring connection epipe")
        except Exception as e:
            self.handle_error(req, conn.sock, conn.client, e)

        return (False, conn)

    def handle_request(self, req, conn):
        environ = {}
        resp = None
        try:
            self.cfg.pre_request(self, req)
            request_start = datetime.now()
            resp, environ = wsgi.create(req, conn.sock, conn.client,
                                        conn.server, self.cfg)
            environ["wsgi.multithread"] = True
            self.nr += 1
            if self.nr >= self.max_requests:
                if self.alive:
                    self.log.info("Autorestarting worker after current request.")
                    self.alive = False
                resp.force_close()

            if not self.alive or not self.cfg.keepalive:
                resp.force_close()
            elif len(self._keep) >= self.max_keepalived:
                resp.force_close()

            respiter = self.wsgi(environ, resp.start_response)
            try:
                if isinstance(respiter, environ['wsgi.file_wrapper']):
                    resp.write_file(respiter)
                else:
                    for item in respiter:
                        resp.write(item)

                resp.close()
            finally:
                request_time = datetime.now() - request_start
                self.log.access(resp, req, environ, request_time)
                if hasattr(respiter, "close"):
                    respiter.close()

            if resp.should_close():
                self.log.debug("Closing connection.")
                return False
        except OSError:
            # pass to next try-except level
            util.reraise(*sys.exc_info())
        except Exception:
            if resp and resp.headers_sent:
                # If the requests have already been sent, we should close the
                # connection to indicate the error.
                self.log.exception("Error handling request")
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                    conn.sock.close()
                except OSError:
                    pass
                raise StopIteration()
            raise
        finally:
            try:
                self.cfg.post_request(self, req, environ, resp)
            except Exception:
                self.log.exception("Exception in post_request hook")

        return True
