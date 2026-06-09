# Companion processes

Gunicorn runs HTTP workers. Many apps also need non-HTTP side processes next to
them: RQ workers, a scheduler, socket.io, a custom daemon. This package lets
Gunicorn supervise those too, so they share the preloaded application memory
(copy-on-write) and one process tree instead of running under a separate
supervisor.

## Architecture

```
        clients (HTTP)
          │
          ▼
 ┌──────────────────────────────────────────────────────────┐
 │ arbiter (master)            preloaded app — shared (COW) │
 └──┬──────────────────┬────────────────────────┬───────────┘
    │ fork             │ fork                   │ fork (after preload)
    ▼                  ▼                        ▼
┌───────────┐     ┌───────────┐         ┌────────────────────┐
│HTTP Worker│ ... │HTTP worker│         │ companion manager  │◀───   control
└───────────┘     └───────────┘         └─────────┬──────────┘      socket (JSON)
                                                  │ fork + supervise       ▲
                                   ┌──────────────┼──────────────┐         │
                                   ▼              ▼              ▼    gunicorn-companion
                              ┌─────────┐    ┌─────────┐    ┌─────────┐    / socat
                              │companion│    │companion│    │companion│
                              │   rq    │    │scheduler│    │socketio │
                              └─────────┘    └─────────┘    └─────────┘
```

The arbiter forks one **companion manager** after `preload_app`. The manager
forks and supervises each configured companion, owns the control socket, and
exits when the arbiter does. It is the only companion-aware part of the arbiter;
all per-process logic lives in the manager. Companions inherit the preloaded
application memory copy-on-write, the same way HTTP workers do.

## States

```
STOPPED ──start──▶ STARTING ──(survives startsecs)──▶ RUNNING
   ▲                                                     │
   │                                            stop / crash
   │                                                     ▼
   └────────────── STOPPED / STOPPING ◀── BACKOFF (unexpected exit)
```

- An unexpected exit goes to `BACKOFF` and restarts after a fixed
  `companion_restart_delay` (no exponential backoff, no retry cap).
- A manual `stop` exits to `STOPPED` and stays there.
- `stop` sends `companion_stop_signal`, then `SIGKILL` after
  `companion_stop_timeout`.

## Configuration

Companions live in the normal Gunicorn config — a Python file you pass with
`-c`. There is no separate companion config file or CLI flag; if you already run
Gunicorn with a config, add the companion settings to it.

Save a `gunicorn.conf.py`:

```python
preload_app = True                                  # required to share memory
companion_control_socket = "/run/gunicorn/companion.sock"
companion_workers = [
    {
        "name": "ticker",
        "target": "myapp.jobs:run",          # callable or "module:attr"
        "stdout": "/var/log/myapp/ticker.log",
        "stderr": "stdout",                   # path, "stdout", or "inherit"
    },
]
```

Start Gunicorn pointing at it; the manager and companions come up with the HTTP
workers:

```sh
gunicorn -c gunicorn.conf.py myapp.wsgi:application
```

`companion_workers` is a list of dicts. `name` and `target` are required; every
other field falls back to the matching global `companion_*` setting, so a dict
only names what differs from the defaults:

| Setting                       | Per-companion key | Meaning                                   |
|-------------------------------|-------------------|-------------------------------------------|
| `companion_control_socket`    | —                 | Unix socket the manager listens on        |
| `companion_cwd`               | `cwd`             | working directory before the target runs  |
| `companion_env`               | `env`             | extra environment variables (merged)      |
| `companion_stop_signal`       | `stop_signal`     | signal sent first on stop (`SIGTERM`)      |
| `companion_stop_timeout`      | `stop_timeout`    | seconds before `SIGKILL`                   |
| `companion_startsecs`         | `startsecs`       | seconds alive to reach `RUNNING`           |
| `companion_restart_delay`     | —                 | seconds before restarting a crash          |
| `companion_stdout`            | `stdout`          | stdout file, or `"inherit"`                |
| `companion_stderr`            | `stderr`          | stderr file, `"stdout"`, or `"inherit"`    |

`target` is either an import string `"module:attr"` or a zero-argument callable.
The child applies `cwd`/`env`, redirects `stdout`/`stderr`, then calls the
target. Log rotation stays external.

## Control

The manager listens on the Unix socket at `companion_control_socket` (0o600,
owned by the user Gunicorn runs as). The protocol is one JSON object per line.

Use the CLI:

```sh
export GUNICORN_COMPANION_SOCKET=/run/gunicorn/companion.sock
gunicorn-companion status
gunicorn-companion stop ticker
gunicorn-companion restart ticker
gunicorn-companion reread          # re-read config; restart only changed companions
```

Or talk to the socket directly:

```sh
echo '{"cmd": "status"}' | socat - UNIX-CONNECT:/run/gunicorn/companion.sock
```

Commands: `status`, `start <name>`, `stop <name>`, `restart <name>`, `reread`.

`reread` is transactional: the new config is validated first, and on any error
nothing changes and the old config keeps running. A `SIGHUP` to Gunicorn
restarts the manager with the reloaded config.

## Files

| File         | Responsibility                                            |
|--------------|-----------------------------------------------------------|
| `config.py`  | `CompanionConfig`, config hash, build configs from cfg    |
| `process.py` | `CompanionProcess` runtime state, public states           |
| `manager.py` | fork/reap, state transitions, restart delay, run loop     |
| `control.py` | Unix socket server and JSON framing                       |
| `ctl.py`     | `gunicorn-companion` command-line client                  |

