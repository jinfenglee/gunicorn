#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

from gunicorn.workers.gthread_routing import SlowRoutePredictor


def test_predictor_unknown_route_is_fast():
    p = SlowRoutePredictor(threshold=1.0)
    assert p.is_slow("GET /") is False


def test_predictor_learns_slow_route_on_update():
    p = SlowRoutePredictor(threshold=1.0, alpha=1.0)
    p.update("GET /slow", 5.0)
    assert p.is_slow("GET /slow") is True
    assert p.is_slow("GET /fast") is False


def test_predictor_ewma_decays_back_to_fast():
    p = SlowRoutePredictor(threshold=1.0, alpha=0.5)
    p.update("GET /x", 5.0)
    assert p.is_slow("GET /x") is True
    # repeated fast samples should pull the EWMA back under the threshold
    for _ in range(20):
        p.update("GET /x", 0.01)
    assert p.is_slow("GET /x") is False


def test_predictor_observe_slow_marks_immediately():
    p = SlowRoutePredictor(threshold=2.0)
    p.observe_slow("POST /report")
    assert p.is_slow("POST /report") is True


def test_predictor_lru_bound():
    p = SlowRoutePredictor(threshold=1.0, max_entries=10)
    for i in range(50):
        p.update("GET /%d" % i, 0.01)
    assert len(p._stats) <= 10


def test_predictor_empty_key_is_fast():
    p = SlowRoutePredictor(threshold=1.0)
    assert p.is_slow(None) is False
    p.update(None, 5.0)  # must not raise
    assert p.is_slow(None) is False
