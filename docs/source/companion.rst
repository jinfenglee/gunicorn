.. _companion:

===================
Companion Processes
===================

Most real deployments run more than HTTP workers. Alongside the web server you
often have background processes: task queues (RQ, Celery), a scheduler, a
websocket / socket.io server, or custom daemons. Normally these are started and
supervised separately with systemd or supervisor.

The **companion process manager** lets Gunicorn run those processes for you, as
children of the same master. They get the same lifecycle as your web workers
and, when you use :ref:`preload-app`, they share the preloaded application
memory through copy-on-write.

Why use it
==========

- **One thing to run.** Web workers and background processes start, stop, and
  reload together under a single Gunicorn command.
- **Less memory.** With ``--preload`` the application is loaded once in the
  master; companions fork from it and share that memory instead of each loading
  their own copy.
- **No drift.** There is one place that owns the lifecycle, so background
  processes don't get out of step with the web workers.

If you only run HTTP workers, you don't need this feature and can ignore it.

How it works
============

Gunicorn forks **one** extra child after preload: the *companion manager*. The
manager forks and supervises each companion you configured. The arbiter only
watches the single manager; the manager handles everything below it.

.. code-block:: text

    gunicorn master  (preloaded app)
      ├── HTTP worker
      ├── HTTP worker
      └── companion manager
            ├── rq-default
            ├── scheduler
            └── socketio

Each companion is just a Python callable you point Gunicorn at. The manager
forks a fresh process, runs the callable, and keeps it alive: if it crashes, the
manager restarts it after a short delay.

Quick start
===========

A companion is configured in your normal Gunicorn config file (the one you pass
with ``-c``). Each entry needs a ``name`` and a ``target``. The target is a
``"module:callable"`` string; the callable takes no arguments and runs the
process (it is expected to block, like a worker's main loop).

.. code-block:: python

    # gunicorn.conf.py
    preload_app = True   # required to share memory with companions

    companion_workers = [
        {"name": "scheduler", "target": "myapp.tasks:run_scheduler"},
        {"name": "rq-default", "target": "myapp.tasks:run_rq", "env": {"QUEUE": "default"}},
    ]

Run Gunicorn as usual::

    gunicorn -c gunicorn.conf.py --preload myapp:application

You'll see the companion manager and each companion start in the logs.

Per-companion options
---------------------

Each entry in ``companion_workers`` may set these keys in addition to ``name``
and ``target``:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Key
     - Meaning
   * - ``cwd``
     - Directory to change into before running the target.
   * - ``env``
     - Extra environment variables, merged onto the inherited env.
   * - ``stop_signal``
     - Signal sent to ask the companion to stop (default ``SIGTERM``).
   * - ``stop_timeout``
     - Seconds to wait after the stop signal before ``SIGKILL``.
   * - ``reload_timeout``
     - Seconds to wait for the old process to exit on restart.
   * - ``startsecs``
     - Seconds a companion must stay up to count as started.
   * - ``stdout``
     - File path for stdout, or ``"inherit"`` (the default).
   * - ``stderr``
     - File path, ``"stdout"`` to merge with stdout, or ``"inherit"``.

Any key you leave out falls back to the matching global setting
(``companion_stop_signal``, ``companion_stop_timeout``, and so on), so you can
set a default once and override it per companion.

Keeping companions in a separate file
--------------------------------------

If you want to change companion specs without touching your web config, put the
``companion_*`` settings in their own Python file and point Gunicorn at it::

    companion_config_file = "/etc/gunicorn/companions.py"

The manager reads its companion settings from that file instead of the main
config.

States
======

A companion is always in one of these states (the same vocabulary as
``supervisorctl``):

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - State
     - Meaning
   * - ``STARTING``
     - Just forked, not yet past ``startsecs``.
   * - ``RUNNING``
     - Up and healthy.
   * - ``BACKOFF``
     - Crashed; waiting ``restart_delay`` seconds before retrying.
   * - ``STOPPING``
     - Was asked to stop; draining before exit.
   * - ``STOPPED``
     - Stopped on purpose and will not auto-restart.

A companion that exits on its own goes to ``BACKOFF`` and is restarted. One you
stop by hand stays ``STOPPED`` until you start it again.

Controlling companions at runtime
==================================

Set a control socket and you can inspect and steer companions while Gunicorn
runs::

    companion_control_socket = "/run/gunicorn/companion.sock"

A small CLI, ``gunicorn-companion``, talks to it::

    gunicorn-companion -s /run/gunicorn/companion.sock status
    gunicorn-companion -s /run/gunicorn/companion.sock restart scheduler
    gunicorn-companion -s /run/gunicorn/companion.sock stop rq-default
    gunicorn-companion -s /run/gunicorn/companion.sock start rq-default

You can also set ``GUNICORN_COMPANION_SOCKET`` instead of passing ``-s`` every
time. The protocol is plain newline-delimited JSON, so ``socat`` works too::

    echo '{"cmd": "status"}' | socat - UNIX-CONNECT:/run/gunicorn/companion.sock

Commands:

- ``status`` — show every companion's state.
- ``start <name>`` / ``stop <name>`` / ``restart <name>`` — act on one.
- ``reread`` — re-read the config file and apply only what changed: new
  companions start, removed ones stop, changed ones restart, untouched ones are
  left alone. It is transactional — if the new config is invalid, nothing
  changes and the old one keeps running.

The socket is created mode ``0o600`` (owner only). Change it with
``companion_control_socket_mode`` if you need group access.

Reload and shutdown
===================

**Reload (SIGHUP).** A reload recycles your HTTP workers and re-reads config.
The companion manager is restarted **only if the companion config actually
changed** — an ordinary web reload leaves your companions running untouched, so
it stays fast. Note that, just like HTTP workers under ``--preload``, companions
pick up new *application code* only on a full restart, not on ``SIGHUP``. For
fine-grained changes without a full reload, use the ``reread`` command.

**Shutdown (SIGTERM).** Gunicorn asks the manager to stop, which sends each
companion its ``stop_signal`` and waits up to ``stop_timeout`` before forcing it
down with ``SIGKILL``. Gunicorn gives the manager enough time to drain all its
companions before it gives up; tune that with
``companion_manager_stop_timeout`` (or it is derived from the slowest companion
plus ``companion_manager_shutdown_buffer``).

Limitations
===========

- **Hot upgrade (USR2) is not supported with companions.** During a ``USR2``
  upgrade the old and new masters run side by side, so each runs its own
  companion manager and every companion runs twice — bad for singletons like a
  scheduler. Restart the master instead of using ``USR2`` when companions are
  configured, or keep singletons out of the companion set. A ``SIGHUP`` reload
  is fine.
- **Linux is the primary target.** Orphan cleanup uses ``prctl`` on Linux, with
  a portable parent-watch fallback elsewhere.

See the :ref:`settings` page for every ``companion_*`` option.
