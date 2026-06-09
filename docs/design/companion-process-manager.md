
Status: proposal / draft
Author: Tanmoy Sarkar  
Scope: `gunicorn/arbiter.py`, `gunicorn/config.py`, `gunicorn/companion/`

## 1. Problem

A Frappe deployment is not only HTTP workers.

Alongside Gunicorn, we usually run persistent non-HTTP processes:

- RQ worker pools
- scheduler
- socket.io / websocket server
- custom background daemons

Today these are usually managed separately through supervisor/systemd.

That causes:

- repeated app memory usage
- separate lifecycle for web and side processes
- reload drift between HTTP workers and background processes
- inconsistent shutdown behavior
- harder production process control

With `preload_app=True`, Gunicorn workers already share preloaded app memory using copy-on-write. The goal is to give non-HTTP processes the same lifecycle and memory-sharing benefit without making them HTTP workers.

## 2. Goal

Gunicorn manages one extra child process: the **Companion Manager**.

The Companion Manager manages all configured companion processes.

```text
gunicorn master
  ├── HTTP worker
  ├── HTTP worker
  └── companion manager
        ├── rq-default
        ├── rq-long
        ├── scheduler
        └── socketio
```

Core rule:

```text
Gunicorn Arbiter manages one Companion Manager.
Companion Manager manages companion processes.
Each companion process manages its own internals.
```

## 3. Architecture

```text
                       gunicorn master
                       preload_app=True
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
   HTTP worker           HTTP worker        companion manager
   serves HTTP           serves HTTP        manages companions
                                                │
                         ┌──────────────────────┼──────────────────────┐
                         │                      │                      │
                         ▼                      ▼                      ▼
                    rq-default              scheduler              socketio
```

Memory sharing still works:

```text
gunicorn master preloads app
  └── forks companion manager
        └── forks rq / scheduler / socketio
```

The manager is forked from the preloaded master. Companion processes are forked from the manager, so they can inherit preloaded application memory.

## 4. Responsibility Boundary

### Gunicorn Arbiter

The Arbiter should:

- start the Companion Manager
- restart it if it crashes
- stop it during Gunicorn shutdown
- ask it to `reread` config when needed
- avoid per-companion process logic

### Companion Manager

The manager should:

- load and validate companion config
- spawn/reap companions
- stop/start/restart companions
- restart unexpected exits after a fixed delay
- track state and expose `status`
- expose a Unix control socket
- redirect stdout/stderr
- apply env and cwd
- log lifecycle events

### Companion Process

A companion runs the actual service, such as RQ, scheduler, socket.io, or a custom daemon.

The companion process owns its own internals:

- signal handling
- job draining
- child workers
- sockets
- event loops

## 5. Companion Is Not an HTTP Worker

A companion must not:

- serve Gunicorn HTTP traffic
- use Gunicorn listener sockets
- use Gunicorn worker heartbeat files
- trigger HTTP worker boot-error halt behavior
- call HTTP worker lifecycle hooks

If a companion exits with `WORKER_BOOT_ERROR` or `APP_LOAD_ERROR`, the web tier must not halt. The manager treats it as a normal companion exit.

## 6. Configuration

Use dict-based config.

```python
preload_app = True

companion_config_file = "/home/frappe/frappe-bench/companion.conf.py"
companion_control_socket = "/run/gunicorn/companion.sock"

companion_workers = [
    {
        "name": "rq-default",
        "target": "frappe_companions:start_rq_default",
        "cwd": "/home/frappe/frappe-bench",
        "env": {"QUEUE": "default"},
        "stop_signal": "SIGTERM",
        "stop_timeout": 300,
        "reload_timeout": 60,
        "stdout": "/var/log/frappe/rq-default.log",
        "stderr": "/var/log/frappe/rq-default.error.log",
    },
    {
        "name": "socketio",
        "target": "frappe_companions:start_socketio",
        "cwd": "/home/frappe/frappe-bench",
        "stop_signal": "SIGTERM",
        "stop_timeout": 60,
        "reload_timeout": 30,
        "stdout": "/var/log/frappe/socketio.log",
        "stderr": "/var/log/frappe/socketio.error.log",
    },
]
```

Global defaults:

```python
companion_stop_signal = "SIGTERM"
companion_stop_timeout = 60
companion_reload_timeout = 60

companion_stdout = None
companion_stderr = None
companion_cwd = None
companion_env = {}

companion_startsecs = 1
companion_restart_delay = 5

# seconds; used when manager timeout is computed dynamically
companion_manager_shutdown_buffer = 10
companion_manager_stop_timeout = None
companion_manager_reload_timeout = None

companion_control_socket_mode = 0o600
```

If manager timeouts are unset, compute them dynamically:

```text
manager_stop_timeout = max(companion.stop_timeout) + companion_manager_shutdown_buffer
manager_reload_timeout = max(companion.reload_timeout) + companion_manager_shutdown_buffer
```

## 7. Config Fields

Required:

| Field    | Meaning                                 |
| -------- | --------------------------------------- |
| `name`   | Unique process name                     |
| `target` | Zero-argument callable or import string |

Optional:

| Field            | Meaning                                                                    |
| ---------------- | -------------------------------------------------------------------------- |
| `cwd`            | Working directory before target                                            |
| `env`            | Extra environment variables                                                |
| `stop_signal`    | Signal used on stop                                                        |
| `stop_timeout`   | Max wait during shutdown                                                   |
| `reload_timeout` | Max wait during restart/reread                                             |
| `stdout`         | Stdout log file or inherit                                                 |
| `stderr`         | Stderr log file, `stdout`, or inherit                                      |
| `startsecs`      | Seconds process must survive before `RUNNING`; makes `STARTING` meaningful |

Validation must reject unknown keys, duplicate names, invalid signals/timeouts, invalid stdout/stderr values, and targets that are not zero-argument callables/import strings.

Not supported: groups, disable/fatal state, max restart count, exponential backoff, process groups, per-companion user switching, HTTP/TCP health checks, process-specific RQ/socket.io behavior.

## 8. Public States

Status should mimic `supervisorctl status`.

```text
STOPPED
STARTING
RUNNING
BACKOFF
STOPPING
```

| State      | Meaning                                                                      |
| ---------- | ---------------------------------------------------------------------------- |
| `STOPPED`  | Manually stopped or not started                                              |
| `STARTING` | Forked, but has not survived `startsecs`                                     |
| `RUNNING`  | Alive and survived `startsecs`                                               |
| `BACKOFF`  | Exited unexpectedly; will restart after `companion_restart_delay`            |
| `STOPPING` | Stop is in progress, from first signal through optional `SIGKILL` until exit |

No public `EXITED`, `UNKNOWN`, or `FATAL`.

Exit metadata is tracked separately:

```text
last_exit_code
last_exit_signal
last_exited_at
exit_count
```

## 9. State Transitions

```text
STOPPED
  └─ start
      → STARTING

STARTING
  ├─ survives startsecs
  │   → RUNNING
  ├─ exits unexpectedly
  │   → BACKOFF
  └─ stop / restart / removed-by-reread
      → STOPPING

RUNNING
  ├─ exits unexpectedly
  │   → BACKOFF
  └─ stop / restart / removed-by-reread
      → STOPPING

BACKOFF
  ├─ retry timer expires
  │   → STARTING
  └─ stop
      → STOPPED

STOPPING
  ├─ process exits
  │   → STOPPED
  └─ timeout exceeded
      → SIGKILL
      → STOPPED
```

When `waitpid` reaps a child, the manager records exit metadata and immediately moves to the next public state.

Early exit during `STARTING` and unexpected exit after `RUNNING` both use the same fixed restart delay.

## 10. Restart Behavior

Configured companions are expected to stay running.

Unexpected exit:

```text
record exit metadata
state = BACKOFF
next_retry_at = now + companion_restart_delay
restart after companion_restart_delay
```

Default:

```python
companion_restart_delay = 5
```

There is no exponential backoff, max restart count, disable state, or fatal state.

A configured process restarts forever unless:

- manually stopped
- removed from config by `reread`
- Gunicorn is stopping/reloading

## 11. Control Socket

The manager exposes a Unix domain socket:

```python
companion_control_socket = "/run/gunicorn/companion.sock"
```

Default permissions:

```python
companion_control_socket_mode = 0o600
```

Gunicorn runs as a non-root user, so the socket is owned by that user and no
group ownership switching is supported.

Protocol: newline-delimited JSON.

Commands:

```text
status
reread
start <name>
stop <name>
restart <name>
```

The manager creates the socket before entering the main loop. During full manager replacement, clients should retry on `ENOENT`, `ECONNREFUSED`, or timeout.

## 12. Command Semantics

### `status`

Request:

```json
{"cmd": "status"}
```

Human output should mimic `supervisorctl status`:

```text
rq-default                      RUNNING   pid 1234, uptime 2 days, 03:12:44
rq-long                         BACKOFF   exited with status 1, retrying in 3s
scheduler                       STOPPED   stopped manually
```

JSON response:

```json
{
  "ok": true,
  "companions": [
    {
      "name": "rq-default",
      "state": "RUNNING",
      "pid": 1234,
      "description": "pid 1234, uptime 2 days, 03:12:44"
    },
    {
      "name": "rq-long",
      "state": "BACKOFF",
      "pid": null,
      "description": "exited with status 1, retrying in 3s",
      "next_retry_at": 1730000000,
      "restart_delay": 5,
      "last_exit_code": 1
    }
  ]
}
```

### `start <name>`

```json
{"cmd": "start", "name": "rq-default"}
```

Uses latest validated config.

```text
STOPPED  -> clear manual_stop, start now
BACKOFF  -> cancel pending retry, clear manual_stop, start now
RUNNING  -> success: already running
STARTING -> success: already starting
STOPPING -> error: process is stopping; poll status and retry
```

### `stop <name>`

```json
{"cmd": "stop", "name": "rq-default"}
```

```text
RUNNING  -> send stop_signal, wait stop_timeout, SIGKILL if needed, STOPPED
STARTING -> send stop_signal, wait stop_timeout, SIGKILL if needed, STOPPED
BACKOFF  -> cancel pending retry, STOPPED
STOPPED  -> success: already stopped
STOPPING -> success: already stopping
```

`stop` sets `manual_stop = True`.

If stopping while `STARTING`, `stop_timeout` governs the stop window, not `startsecs`.

### `restart <name>`

```json
{"cmd": "restart", "name": "rq-default"}
```

```text
RUNNING  -> clear manual_stop, stop using reload_timeout, start
STARTING -> enter STOPPING, stop current child using reload_timeout, start
BACKOFF  -> cancel pending retry, clear manual_stop, start immediately
STOPPED  -> clear manual_stop, start immediately
STOPPING -> error: process is stopping; poll status and retry
```

`restart` does not reread config.

### `reread`

```json
{"cmd": "reread"}
```

Transactional config reload:

```text
new process       -> add and start
removed process   -> stop and remove
changed process   -> update config; restart unless manual_stop=True
unchanged process -> keep current state
```

If a manually stopped process changes config:

```text
update stored config
keep STOPPED
next start uses latest config
```

Success:

```json
{
  "ok": true,
  "added": ["new-worker"],
  "removed": ["old-worker"],
  "restarted": ["rq-default"],
  "unchanged": ["socketio"]
}
```

`unchanged` means no process action was taken. It may include manually stopped companions whose config changed; the new config is accepted and stored, and the next `start <name>` uses it.

Failure:

```json
{
  "ok": false,
  "error": "invalid config: duplicate companion name rq-default",
  "kept_old_config": true
}
```

`kept_old_config=true` means no running process was changed and previous validated config remains active.

## 13. Reread Diff

Use one stable config hash per companion.

```text
new name       -> add/start
missing name   -> stop/remove
hash changed   -> update config; restart unless manual_stop=True
hash unchanged -> no process action
```

This intentionally restarts even if only `stop_timeout`, `stdout`, or `env` changes. Simpler and easier to test.

`reread` flow:

1. Read config file.
2. Extract companion settings.
3. Validate full config.
4. Compute one config hash per companion.
5. Diff old/new config.
6. Apply only if validation succeeds.

Prefer a dedicated config file:

```python
companion_config_file = "/home/frappe/frappe-bench/companion.conf.py"
```

If unset, the manager may fall back to Gunicorn config file, but must read only companion settings.

## 14. stdout/stderr, env, cwd

### stdout/stderr

```python
"stdout": "/var/log/frappe/rq-default.log",
"stderr": "/var/log/frappe/rq-default.error.log",
```

Allowed:

```python
None
"inherit"
"stdout"   # only for stderr
"/path/to/file.log"
```

The companion child opens stdout/stderr after fork and before `target()`.

Files are opened in append mode.

Log rotation is external:

- `copytruncate` works without restart
- `create`/rename rotation needs companion restart
- live fd reopen for already-running companions is out of scope

### env/cwd

Before `target()`:

```python
os.chdir(cwd)
os.environ.update(env)
```

Changing stdout/stderr/env/cwd changes the config hash and causes restart unless manually stopped.

## 15. File Descriptors

Manager child must close Gunicorn-only fds:

- master signal pipe
- HTTP listener sockets
- worker heartbeat tmp files

Companion children must close manager-only fds before running target.

Companions must not keep Gunicorn HTTP listener sockets open.

## 16. Parent Death / Orphan Cleanup

Manager exits if Gunicorn master dies.

Linux:

```python
prctl(PR_SET_PDEATHSIG, SIGTERM)
```

Non-Linux fallback:

```text
manager records parent pid
manager checks os.getppid() every 5 seconds
if os.getppid() returns 1, manager exits
```

Companion children should also use parent-death signal where available. Without Linux `prctl`, cleanup after manager death is best-effort because target code takes over.

## 17. Internal State

Maintain enough state for `status`:

- name
- state
- pid
- uptime
- restart count
- exit count
- last exit code/signal
- last started/exited time
- next retry time
- stop timeout kills
- manual stop flag
- stdout/stderr path

No Prometheus exporter inside the manager.

## 18. Implementation Layout

```text
gunicorn/companion/
  __init__.py
  config.py
  process.py
  manager.py
  control.py
```

`config.py`:

- load config
- validate config
- normalize defaults
- compute config hash

`process.py`:

- `CompanionConfig`
- `CompanionProcess`
- state model

`manager.py`:

- run loop
- spawn/reap
- start/stop/restart
- fixed restart delay
- state transitions
- stdout/stderr/env/cwd setup

`control.py`:

- Unix socket server
- JSON command parser
- JSON response writer

## 19. Arbiter Changes

Keep Arbiter changes small:

- manager state
- spawn manager
- reap manager
- stop manager
- reload/reread manager
- helper to call control socket if needed

No per-companion logic in Arbiter.

## 20. Implementation Tasks

- [x] Add companion config settings in `gunicorn/config.py`.
- [x] Add config validation for `companion_workers`.
- [x] Add `CompanionConfig` and config hash generation.
- [x] Add public process states.
- [x] Add `CompanionProcess` runtime state.
- [x] Add status description helpers.
- [x] Add `CompanionManager` skeleton.
- [x] Spawn one companion process from the manager.
- [x] Apply `cwd` and `env` before target.
- [x] Redirect `stdout` and `stderr`.
- [x] Reap exited companion processes.
- [ ] Implement `STARTING -> RUNNING` using `startsecs`.
- [ ] Implement `BACKOFF` with fixed `companion_restart_delay`.
- [ ] Implement `start_process`.
- [ ] Implement `stop_process`.
- [ ] Implement `restart_process`.
- [ ] Preserve and clear `manual_stop` correctly.
- [ ] Add Unix control socket.
- [ ] Implement JSON command protocol.
- [ ] Implement `status`.
- [ ] Implement `start`.
- [ ] Implement `stop`.
- [ ] Implement `restart`.
- [ ] Implement transactional `reread`.
- [ ] Add manager spawn/reap logic in Arbiter.
- [ ] Add manager shutdown handling in Arbiter.
- [ ] Wire Gunicorn reload to manager `reread` or restart.
- [ ] Close Gunicorn-only fds in manager child.
- [ ] Close manager-only fds in companion child.
- [ ] Add parent-death cleanup.
- [ ] Add lifecycle logs.
- [ ] Add tests for config validation.
- [ ] Add tests for state transitions.
- [ ] Add tests for control commands.
- [ ] Add tests for transactional reread.
- [ ] Add tests that HTTP worker behavior is unchanged.

## 21. Test Plan

Test:

- config validation
- config hash diff
- transactional reread
- `reread` success/failure response
- manual stop + reread behavior
- `start`, `stop`, `restart` on all public states
- control socket commands and permissions
- control socket unavailable retry behavior
- supervisord-like status output
- state transitions
- manager lifecycle from Arbiter
- companion spawn/reap
- fixed 5s restart delay
- `startsecs` behavior
- stdout/stderr redirection
- env and cwd
- fd cleanup
- parent-death cleanup
- HTTP worker behavior unchanged

## 22. Out of Scope

Not supported:

- groups
- dependency ordering
- process group killing
- disable/fatal state
- max restart count
- exponential backoff
- CLI config for companion specs
- RQ/socket.io/scheduler-specific behavior
- per-companion user switching
- HTTP/TCP/custom health checks
- live log fd reopen for already-running companions

## 23. Summary

Use a Companion Manager, not direct companion management inside Arbiter.

This gives:

- shared memory through `preload_app=True`
- small Arbiter changes
- supervisord-like process management and status
- controlled `start`, `stop`, `restart`, `reread`, `status`
- transactional config reread
- fixed restart delay
- simple process-running health
- per-companion env/cwd/stdout/stderr
- simple public state machine
- safer shutdown/reload behavior
