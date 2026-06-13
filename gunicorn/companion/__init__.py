#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

"""Companion process manager.

Gunicorn manages one extra child, the Companion Manager, which manages all
configured non-HTTP companion processes (RQ workers, scheduler, socket.io,
custom daemons). See ``docs/design/companion-process-manager.md``.
"""
