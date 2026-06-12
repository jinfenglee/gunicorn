#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import pytest

from gunicorn.config import Config, validate_companion_workers
from gunicorn.companion.config import CompanionConfig, build_companion_configs


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


def test_build_applies_global_restart_delay():
    cfg = make_config(
        [{"name": "rq", "target": "pkg:run"}],
        companion_restart_delay=42)
    config, = build_companion_configs(cfg)
    assert config.restart_delay == 42


def test_build_rejects_unknown_spec_key():
    with pytest.raises(ValueError):
        build_companion_configs(
            make_config([{"name": "rq", "target": "pkg:run", "stop_sigal": "X"}]))


def test_build_reads_companion_config_file(tmp_path):
    config_file = tmp_path / "companion.conf.py"
    config_file.write_text(
        'companion_workers = [{"name": "scheduler", "target": "app:run"}]\n'
        'companion_stop_signal = "SIGINT"\n')
    cfg = make_config([], companion_config_file=str(config_file))
    config, = build_companion_configs(cfg)
    assert config.name == "scheduler"
    assert config.stop_signal == "SIGINT"


def test_build_empty_when_none_configured():
    assert build_companion_configs(make_config([])) == []


def test_build_requires_name_and_target():
    with pytest.raises(ValueError):
        build_companion_configs(make_config([{"name": "rq"}]))
    with pytest.raises(ValueError):
        build_companion_configs(make_config([{"target": "pkg:run"}]))


def test_validate_companion_workers_accepts_none_and_list():
    assert validate_companion_workers(None) == []
    workers = [{"name": "rq", "target": "pkg:run"}]
    assert validate_companion_workers(workers) == workers


def test_validate_companion_workers_rejects_non_list():
    with pytest.raises(TypeError):
        validate_companion_workers("rq")


def test_validate_companion_workers_rejects_non_dict_item():
    with pytest.raises(TypeError):
        validate_companion_workers(["rq"])


def test_config_hash_stable_and_field_sensitive():
    base = CompanionConfig(name="rq", target="pkg:run")
    same = CompanionConfig(name="rq", target="pkg:run")
    changed = CompanionConfig(name="rq", target="pkg:run", stop_timeout=99)
    assert base.config_hash == same.config_hash
    assert base.config_hash != changed.config_hash


def test_config_hash_ignores_restart_delay():
    # restart_delay does not affect the spawned process, so changing it must not
    # change the hash (and so must not trigger a restart on reread).
    base = CompanionConfig(name="rq", target="pkg:run", restart_delay=5)
    changed = CompanionConfig(name="rq", target="pkg:run", restart_delay=30)
    assert base.config_hash == changed.config_hash


def test_config_hash_keys_callable_target_by_qualified_name():
    def run():
        pass

    keyed = CompanionConfig._target_key(run)
    assert ":" in keyed and keyed.endswith("run")
    # A callable target hashes stably across CompanionConfig instances.
    assert (CompanionConfig(name="rq", target=run).config_hash
            == CompanionConfig(name="rq", target=run).config_hash)
