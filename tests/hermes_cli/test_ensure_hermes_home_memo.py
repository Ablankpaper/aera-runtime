"""``ensure_hermes_home`` repairs every active home on every call.

Aera deliberately does not memoize this process-lifetime check: profiles may
be repaired or switched while the Runtime stays alive, so both missing
subdirectories and a missing home must be restored on the next config load.
"""

import shutil

from hermes_cli import config as cfg


def test_repeat_calls_repair_deleted_subdir_and_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    cfg.ensure_hermes_home()
    assert (home / "sessions").is_dir()

    # A later config load repairs a removed required subdirectory even though
    # the home itself still exists.
    shutil.rmtree(home / "sessions")
    cfg.ensure_hermes_home()
    assert (home / "sessions").is_dir()

    # A vanished home also restores the full skeleton.
    shutil.rmtree(home)
    cfg.ensure_hermes_home()
    assert (home / "sessions").is_dir()


def test_distinct_home_paths_each_get_the_skeleton(tmp_path, monkeypatch):
    first = tmp_path / "a" / ".hermes"
    second = tmp_path / "b" / ".hermes"

    monkeypatch.setenv("HERMES_HOME", str(first))
    cfg.ensure_hermes_home()

    # Profile switch: HERMES_HOME moves → the new path is ensured too.
    monkeypatch.setenv("HERMES_HOME", str(second))
    cfg.ensure_hermes_home()

    assert (first / "logs").is_dir()
    assert (second / "logs").is_dir()
