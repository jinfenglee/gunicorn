#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import pytest

from gunicorn.config import Config
from gunicorn.companion.config import build_companion_configs


def make_config(workers, **overrides):
    cfg = Config()
    cfg.set("companion_workers", workers)
    for key, value in overrides.items():
        cfg.set(key, value)
    return cfg


def test_build_applies_global_defaults():
    cfg = make_config(
        [{"name": "rq", "target": "pkg:run"}],
        companion_stop_signal="SIGINT",
        companion_startsecs=5)
    config, = build_companion_configs(cfg)
    assert config.name == "rq"
    assert config.target == "pkg:run"
    assert config.stop_signal == "SIGINT"
    assert config.startsecs == 5


def test_build_per_spec_overrides_global():
    cfg = make_config(
        [{"name": "rq", "target": "pkg:run", "stop_signal": "SIGTERM"}],
        companion_stop_signal="SIGINT")
    config, = build_companion_configs(cfg)
    assert config.stop_signal == "SIGTERM"


def test_build_empty_when_none_configured():
    assert build_companion_configs(make_config([])) == []


def test_build_requires_name_and_target():
    with pytest.raises(ValueError):
        build_companion_configs(make_config([{"name": "rq"}]))
    with pytest.raises(ValueError):
        build_companion_configs(make_config([{"target": "pkg:run"}]))
