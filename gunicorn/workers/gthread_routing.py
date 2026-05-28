#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

"""Slow-route prediction for the gthread worker.

The :class:`SlowRoutePredictor` decides, before a request is handed to a
worker, whether its route is expected to be slow, based on previously observed
timings of the same route (method + path). The gthread worker uses this to
route slow requests to a dedicated thread pool so they cannot starve fast
requests.
"""

import threading
from collections import OrderedDict


class SlowRoutePredictor:
    """Predicts whether a route (method + path) is slow.

    Timings are tracked per route as an exponentially weighted moving average
    (EWMA) so that a route which becomes fast again decays back below the
    threshold. The table is bounded (LRU) to cap memory under high route
    cardinality.
    """

    def __init__(self, threshold, max_entries=1024, alpha=0.3):
        self.threshold = threshold
        self.alpha = alpha
        self.max_entries = max_entries
        self._stats = OrderedDict()
        self._lock = threading.Lock()

    def is_slow(self, key):
        if not key:
            return False
        with self._lock:
            ewma = self._stats.get(key)
            if ewma is None:
                return False
            self._stats.move_to_end(key)
            return ewma >= self.threshold

    def update(self, key, duration):
        """Record an observed processing ``duration`` (seconds) for ``key``."""
        if not key:
            return
        with self._lock:
            ewma = self._stats.get(key)
            if ewma is None:
                ewma = duration
            else:
                ewma = (1 - self.alpha) * ewma + self.alpha * duration
            self._stats[key] = ewma
            self._stats.move_to_end(key)
            self._evict()

    def observe_slow(self, key):
        """Mark ``key`` slow now, before its request has finished.

        Used when an in-flight request crosses the threshold, so the rest of a
        simultaneous burst to a never-seen slow route is routed to the slow lane
        without waiting for the first request to complete.
        """
        if not key:
            return
        with self._lock:
            cur = self._stats.get(key, 0.0)
            self._stats[key] = max(cur, self.threshold)
            self._stats.move_to_end(key)
            self._evict()

    def _evict(self):
        while len(self._stats) > self.max_entries:
            self._stats.popitem(last=False)
