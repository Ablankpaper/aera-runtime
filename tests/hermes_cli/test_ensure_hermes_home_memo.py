"""Behavior tests for the per-home ensure_hermes_home fast path."""

import shutil

from hermes_cli import config as cfg


def test_repeat_calls_are_memoized_but_deleted_home_is_recreated(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    cfg.ensure_hermes_home()
    assert (home / "sessions").is_dir()

    shutil.rmtree(home / "sessions")
    cfg.ensure_hermes_home()
    assert not (home / "sessions").exists()

    shutil.rmtree(home)
    cfg.ensure_hermes_home()
    assert (home / "sessions").is_dir()


def test_distinct_home_paths_each_get_the_skeleton(tmp_path, monkeypatch):
    first = tmp_path / "a" / ".hermes"
    second = tmp_path / "b" / ".hermes"

    monkeypatch.setenv("HERMES_HOME", str(first))
    cfg.ensure_hermes_home()

    monkeypatch.setenv("HERMES_HOME", str(second))
    cfg.ensure_hermes_home()

    assert (first / "logs").is_dir()
    assert (second / "logs").is_dir()
