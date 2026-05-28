# Design: Slow-request isolation for the gthread worker (predictive dual-queue)

Status: proposal / draft
Author: (ankush)
Scope: `gunicorn/workers/gthread.py`, `gunicorn/config.py`

## 1. Problem

The `gthread` worker runs synchronous WSGI applications on a single
`ThreadPoolExecutor` sized to `cfg.threads` (`gthread.py:95-97`). Every accepted
connection is submitted to that one pool (`enqueue_req`, `gthread.py:117-121`).
Because the pool has a fixed number of threads and an unbounded work queue, a
flood of slow requests occupies every thread and all fast requests starve behind
them in the queue — head-of-line blocking.

Goal: **route requests that are predicted to be slow into a separate, dedicated
lane so they can never occupy the threads reserved for fast requests, even under
a flood.** Fast requests go to a fast lane; slow requests go to a slow lane. The
slow lane may help drain fast work when its own queue is empty, but the fast
lane never touches slow work.

This supersedes the earlier "demotion-only" proposal, which could not stop slow
work from entering the fast pool and therefore could not survive a flood.

## 2. Why prediction is required (and its hard limit)

You cannot preempt a running Python thread executing WSGI code (`gthread.py:352`):
once a slow request is on a thread, that thread is committed until the app
returns. So isolation has to happen **before** a request is handed to a worker —
i.e. at routing time. That means we must decide "fast or slow" from the request
*before* running it.

The only information available pre-execution is the request itself (method,
path, headers) plus what we have learned from prior requests. So the design is:

1. A **predictor** that, given a request's route, answers "slow?" using learned
   per-route timing statistics (plus optional operator-seeded patterns).
2. **Routing at accept time** based on that prediction, into one of two pools.
3. **Learning**: every completed request — and any request that crosses the
   slow threshold mid-flight — updates the predictor, so a slow route is
   recognized after its first occurrence(s) and all subsequent traffic to it is
   routed to the slow lane.

Hard limit to state up front: a route that has **never been seen** cannot be
predicted slow on its very first request(s); those first occurrences run in the
fast lane until learning kicks in. We minimize this window (§5.4) and let
operators pre-seed known-slow routes (§5.1). For repeated/flooding slow routes —
the actual failure mode — prediction is effective after the first sample.

## 3. Architecture overview

```
                          ┌─────────────────────────────────────────┐
   listener ──accept──▶   │  main loop: poller-driven classification  │
                          │  peek request line ▶ predictor.is_slow?   │
                          └───────────────┬───────────────┬───────────┘
                                          │ fast          │ slow
                                          ▼               ▼
                                   ┌───────────┐   ┌───────────┐
                                   │ fast_pool │   │ slow_pool │ (bounded, 503 on full)
                                   │ F threads │   │ S threads │
                                   └─────┬─────┘   └─────┬─────┘
                                         └───────┬───────┘
                              on completion: predictor.update(route, duration)
```

- **Fast lane**: a `ThreadPoolExecutor` of `F = cfg.threads` threads. Only ever
  runs fast-classified work.
- **Slow lane**: a separate `ThreadPoolExecutor` of `S = cfg.slow_threads`
  threads (default 1). Only ever runs slow-classified work.
- Total OS threads per worker = `F + S`.
- The slow lane is bounded: a counter (`nr_slow`) tracks slow requests
  submitted-but-not-finished; once it reaches `S + cfg.slow_queue_maxsize` (i.e.
  running plus queued), further slow requests are rejected with `503` instead of
  growing the executor's unbounded internal queue. The fast lane is governed by
  the existing `worker_connections` admission like today.

### Why two plain pools (and not a custom dual-queue scheduler)

An earlier revision used a single custom scheduler with two queues and
one-directional **work stealing** (idle slow threads draining the fast queue).
Two independent `ThreadPoolExecutor`s are dramatically simpler and rely on
well-tested stdlib machinery. The one capability given up is work stealing: the
`S` slow threads sit idle when there is no slow work, even if fast work is
queued. For the common case (`S` small, e.g. 1) this is a negligible amount of
parked capacity, and the simplicity is worth it. If maximizing throughput under
pure-fast load ever matters more than simplicity, the custom scheduler can be
reintroduced behind the same `enqueue_req` interface without touching routing.

## 4. Routing point: classify before threading

Today, parsing happens inside the worker thread (`handle` → `next(conn.parser)`,
`gthread.py:295`), which is too late — the request is already on a thread. We
move *classification only* (not full parsing) into the main loop.

### 4.1 Restructured connection lifecycle

Both freshly accepted connections and keepalive connections flow through one
poller-driven classification step (this unifies `accept`/`reuse_connection` and
also moves slow-client header reads off the worker threads — a side benefit
against slowloris):

1. `accept` (`gthread.py:123`): accept socket, create `TConn`, set non-blocking,
   register it in the poller for `EVENT_READ` with a `classify_and_dispatch`
   callback. **Do not submit to any pool yet.** `nr_conns += 1`.
2. When the socket becomes readable, `classify_and_dispatch(conn)`:
   - **Peek** the buffered bytes with `recv(n, socket.MSG_PEEK)` (plaintext) —
     this reads without consuming, so the worker's parser still sees the full
     byte stream unchanged. No parser changes required.
   - Parse just the request line (`METHOD SP PATH SP VERSION CRLF`) from the
     peeked buffer. If the line has not fully arrived yet, return and wait for
     the next readable event (bounded by the existing keepalive/header timeout so
     a stalled client is eventually closed, not left forever).

   > **Why peek the request line, not fully read/parse the request here?**
   > Classification only needs method + path. Doing a *full* read/parse of the
   > request in the main loop is actively harmful: the main loop is a single
   > thread serving every connection (accepts, keepalive, the poller). A blocking
   > full read lets one slow client — slowloris, slow network, or a large/chunked
   > body — stall the **entire worker**, which is strictly worse than the
   > thread-pool starvation we are fixing (there is no pool to absorb it).
   > Peeking only inspects already-buffered bytes and defers to the poller if the
   > line is incomplete, so it never blocks. It also avoids having to read the
   > body in the main loop (WSGI streams `wsgi.input` lazily) and keeps header
   > parsing, parse-error responses (400/414), and `wsgi.input` wiring in the
   > worker where they already live.
   - Compute `route_key` (default: `METHOD + " " + path`, query string stripped;
     overridable via hook, §5.1).
   - `slow = predictor.is_slow(route_key)` (or matches a seeded slow pattern).
   - Unregister the socket from the poller and submit the connection to the
     **slow** pool if `slow` else the **fast** pool.
3. The worker's `handle`/`handle_request` run unchanged. On completion, the
   measured `request_time` (already computed at `gthread.py:362`) is fed to
   `predictor.update(route_key, duration)`.
4. Keepalive: after a kept-alive request, re-register the connection in the
   poller with the same `classify_and_dispatch` callback (instead of the old
   `reuse_connection`), so the *next* request on the connection is re-classified
   independently (it may hit a different route).

### 4.2 SSL connections

Plaintext peek does not work through TLS — the request line is encrypted until
the handshake completes. For SSL connections in this first cut:

- They cannot be pre-classified at the socket level, so they default to the
  **fast** lane and rely on mid-flight + completion learning (§5.4) — meaning an
  SSL-only deployment does not get full flood protection.
- Note in docs that the common production layout terminates TLS upstream (e.g.
  nginx) so gunicorn sees plaintext and gets full protection.
- **Phase 2** (deferred): drive a non-blocking TLS handshake from the poller and
  buffer the decrypted request line (feeding it back via `Unreader.unread`,
  `unreader.py:51`) to classify SSL the same way.

## 5. Components

### 5.1 Config (`gunicorn/config.py`)

New settings, mirroring `WorkerThreads` (`config.py:697`):

- `slow_request_threshold` — float seconds; a route whose learned timing meets/
  exceeds this is "slow". Default e.g. `1.0`. **`0` disables the whole feature**
  and restores today's single-pool behavior exactly.
- `slow_threads` — `S`, slow-lane worker count. Default `1`.
- `slow_queue_maxsize` — bound on `slow_q`; overflow ⇒ `503`. Default e.g. `100`
  (`0` = unbounded).
- `slow_lane_retry_after` — seconds for the `Retry-After` header on 503.

A `slow_route_key` hook to customize the route key (e.g. collapse
`/users/<id>`) is a possible future addition; the default key is method + path
with the query string stripped.

### 5.2 Two thread pools

`init_process` builds two plain `ThreadPoolExecutor`s when routing is enabled —
`fast_pool` (`F = cfg.threads`) and `slow_pool` (`S = cfg.slow_threads`) — and
falls back to the single `get_thread_pool()` executor when it is disabled.
`enqueue_req(conn, slow)` submits to the matching pool; both produce ordinary
`concurrent.futures.Future`s, so `_wrap_future`, `add_done_callback`,
`self.futures` tracking, and `futures.wait` all keep working unchanged.

- Bounding the slow lane: `nr_slow` counts slow requests submitted-but-not-yet-
  finished. `enqueue_req` rejects (503) when `nr_slow >= S + slow_queue_maxsize`;
  `finish_request` decrements it. This caps the slow executor's otherwise
  unbounded internal queue.
- Shutdown drains both pools via a `_shutdown_pools` helper, replacing the
  single `tpool.shutdown` calls; the `graceful_timeout` `futures.wait` is
  unchanged.

### 5.3 Predictor

A small, self-contained, thread-safe object:

- State: bounded LRU map `route_key -> {ewma_seconds, samples, last_seen}`.
  Bounding caps memory under high route cardinality.
- `update(route_key, duration)`: EWMA with decay so a route that becomes fast
  again eventually returns to the fast lane (avoids permanent misclassification
  after a one-off slow spike). Called on every completion.
- `is_slow(route_key)`: `True` if its `ewma_seconds >= slow_request_threshold`.
  Unknown routes ⇒ `False` (fast) by default.
- Optional hysteresis (separate promote/demote thresholds) to avoid flapping
  around the boundary.

### 5.4 Learning signals

1. **Completion (primary)**: feed `request_time` (`gthread.py:362`) into
   `predictor.update`. After a slow route's first request completes, it is known.
2. **Mid-flight observation (catches simultaneous first-bursts)**: the main loop
   already sweeps `self.futures` for the hard timeout (`gthread.py:245-250`). In
   that sweep, for any in-flight request whose elapsed time exceeds
   `slow_request_threshold`, call `predictor.update` with that elapsed time
   *immediately* (do not wait for completion, and do not move the running
   request — we can't). This shortens the learning window when many requests to a
   brand-new slow route arrive at once: subsequent ones in the burst route to the
   slow lane after one threshold interval instead of after a full slow request.

## 6. Behavior under load (the cases that matter)

- **Flood of a previously-seen slow route**: every such request
  is routed to the slow pool. The `F` fast threads are never given this work and
  keep serving fast traffic at full capacity. When the slow lane reaches
  `S + slow_queue_maxsize`, further slow requests get a fast `503` — backpressure
  is contained to the slow lane.
- **Flood of a never-seen slow route**: the first occurrence(s) run in the fast
  lane; mid-flight learning (§5.4.2) flips the route to slow after one threshold
  interval, so the flood is contained quickly.
- **Mixed fast traffic, idle slow lane**: the `S` slow threads stay parked (no
  work stealing in this design — see §3), so fast throughput is `F`, not `F + S`.
- **Misprediction (route marked slow but now fast)**: handled gracefully — it
  runs in the slow lane, and EWMA decay restores it to the fast lane over time.

## 7. Implementation checklist (touch points)

Implemented:

- `config.py` — `slow_request_threshold`, `slow_threads`, `slow_queue_maxsize`,
  `slow_lane_retry_after`, plus `validate_pos_float`.
- `gthread.py` `init_process`/`get_thread_pool` — build `fast_pool` and
  `slow_pool` (or the single legacy pool when disabled); `_shutdown_pools`.
- `gthread.py` `enqueue_req` — route to the matching pool; `nr_slow` bound +
  `reject_overloaded` (503).
- `gthread.py` `accept`/`park_for_request`/`classify_and_dispatch`/
  `_peek_request_line`/`_route_key` — poller-driven request-line peek + routing.
- `gthread.py` `finish_request` — `predictor.update`, `nr_slow` decrement,
  routing-aware keepalive re-park.
- `gthread.py` run-loop sweep — mid-flight learning.
- `gthread_routing.py` — `SlowRoutePredictor`.

## 8. Backward compatibility

- `slow_request_threshold = 0` ⇒ feature off: single pool, no classification, no
  rejection — byte-for-byte current behavior.
- Hard per-request timeout (`gthread.py:243-250`) preserved unchanged; this adds
  a softer, non-fatal classification on top.
- Worker `handle`/`handle_request`, keepalive semantics, and the
  future/`finish_request` contract are preserved (MSG_PEEK leaves the byte
  stream intact, so the parser is untouched).

## 9. Test plan

- **Predictor unit**: unknown ⇒ fast; after `update` with a slow duration ⇒
  slow; EWMA decay restores fast; seeded patterns are slow from first call; LRU
  bound holds under many keys.
- **Routing unit**: `classify_and_dispatch` extracts the right `route_key` from
  partial vs complete peeked buffers; incomplete line defers; complete line
  dispatches to the expected lane.
- **Integration — flood isolation**: app with a known-slow route flooded
  concurrently; assert fast-route latency stays low and slow requests never
  occupy fast workers; assert 503 once the slow lane is full.
- **Integration — cold start**: never-seen slow route burst ⇒ confirm the lane
  flips to slow within ~one threshold interval via mid-flight learning.
- **Regression**: `slow_request_threshold = 0` ⇒ current behavior; keepalive,
  SSL, and graceful shutdown paths still pass existing tests.
