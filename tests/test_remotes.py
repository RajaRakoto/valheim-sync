"""Tests for multi-admin remote switching (credentials, registry, CLI)."""

from __future__ import annotations

import pytest

import sync


def _feed(monkeypatch: pytest.MonkeyPatch, key_id: str = "kid", secret: str = "secret-key") -> None:
    monkeypatch.setattr("builtins.input", lambda *a: key_id)
    monkeypatch.setattr(sync.getpass, "getpass", lambda *a: secret)


def test_remote_add_and_list(env, monkeypatch, capsys) -> None:
    env["make_config"]()
    _feed(monkeypatch)

    assert sync.main(["remote", "add", "adminB", "bucketB/team-b"]) == 0

    assert "adminB" in sync.rclone_remote_names()
    assert sync.load_remotes()["adminB"] == "bucketB/team-b"
    cfg = sync.load_config()
    assert cfg.remote == "adminB"
    assert cfg.remote_base == "bucketB/team-b"
    conf = sync.rclone_conf_path().read_text(encoding="utf-8")
    assert "kid" in conf
    assert "secret-key" in conf

    assert sync.main(["remote", "list"]) == 0
    out = capsys.readouterr().out
    assert "adminB" in out
    assert "secret-key" not in out


def test_remote_add_two_remotes_coexist(env, monkeypatch) -> None:
    env["make_config"]()
    _feed(monkeypatch)

    assert sync.main(["remote", "add", "a1", "b1/x"]) == 0
    assert sync.main(["remote", "add", "a2", "b2/y"]) == 0

    assert {"a1", "a2"} <= set(sync.rclone_remote_names())
    assert sync.load_config().remote == "a2"
    assert sync.load_remotes() == {"a1": "b1/x", "a2": "b2/y"}


def test_remote_use_switches(env, monkeypatch) -> None:
    env["make_config"]()
    _feed(monkeypatch)
    sync.main(["remote", "add", "a1", "b1/x"])
    sync.main(["remote", "add", "a2", "b2/y"])

    assert sync.main(["remote", "use", "a1"]) == 0

    cfg = sync.load_config()
    assert cfg.remote == "a1"
    assert cfg.remote_base == "b1/x"


def test_remote_use_creates_missing(env, monkeypatch) -> None:
    env["make_config"]()
    _feed(monkeypatch)

    assert sync.main(["remote", "use", "fresh", "b/f"]) == 0

    assert "fresh" in sync.rclone_remote_names()
    assert sync.load_remotes()["fresh"] == "b/f"
    assert sync.load_config().remote == "fresh"


def test_remote_use_reuses_stored_base(env, monkeypatch) -> None:
    env["make_config"]()
    _feed(monkeypatch)
    sync.main(["remote", "add", "a1", "b1/x"])
    sync.main(["remote", "add", "a2", "b2/y"])

    assert sync.main(["remote", "use", "a1"]) == 0
    assert sync.load_config().remote_base == "b1/x"


def test_switch_alias(env, monkeypatch) -> None:
    env["make_config"]()
    _feed(monkeypatch)

    assert sync.main(["switch", "adminB", "b/x"]) == 0

    assert sync.load_config().remote == "adminB"
    assert sync.load_config().remote_base == "b/x"


def test_remote_remove(env, monkeypatch, capsys) -> None:
    env["make_config"]()
    _feed(monkeypatch)
    sync.main(["remote", "add", "adminB", "b/x"])

    assert sync.main(["remote", "remove", "adminB"]) == 0

    assert "adminB" not in sync.rclone_remote_names()
    assert "adminB" not in sync.load_remotes()
    assert "active remote" in capsys.readouterr().err


def test_remote_remove_unknown(env) -> None:
    env["make_config"]()
    assert sync.main(["remote", "remove", "nope"]) == 1


def test_remote_add_invalid_name(env) -> None:
    env["make_config"]()
    assert sync.main(["remote", "add", "bad name"]) == 1


def test_write_rclone_remote_force(env) -> None:
    cfg = sync.Config(remote="r1")
    sync.write_rclone_remote(cfg, "kid1", "key1")
    sync.write_rclone_remote(cfg, "kid1", "key1")
    assert sync.rclone_conf_path().read_text(encoding="utf-8").count("[r1]") == 1

    sync.write_rclone_remote(cfg, "kid2", "key2", force=True)
    text = sync.rclone_conf_path().read_text(encoding="utf-8")
    assert text.count("[r1]") == 1
    assert "key2" in text
    assert "key1" not in text


def test_remove_rclone_remote_keeps_others(env) -> None:
    sync.write_rclone_remote(sync.Config(remote="a"), "k", "v")
    sync.write_rclone_remote(sync.Config(remote="b"), "k", "v")

    assert sync.remove_rclone_remote("a") is True
    assert sync.rclone_remote_names() == ["b"]
    assert sync.remove_rclone_remote("a") is False


def test_set_cloud_records_base(env) -> None:
    env["make_config"]()
    assert sync.main(["set-cloud", "valheim:bucket/Other"]) == 0
    assert sync.load_remotes()["valheim"] == "bucket/Other"


def test_load_remotes_not_object(env) -> None:
    env["make_config"]()
    sync.remotes_path().write_text("[]", encoding="utf-8")
    with pytest.raises(sync.SyncError):
        sync.load_remotes()


def test_validate_remote_name() -> None:
    assert sync.validate_remote_name("team-a_1.2") == "team-a_1.2"
    with pytest.raises(sync.SyncError):
        sync.validate_remote_name("bad name")


def test_switch_targets_other_cloud(env, monkeypatch) -> None:
    valheim = env["make_config"]()
    _feed(monkeypatch)
    (valheim / "worlds_local" / "Midgard").mkdir(parents=True)
    (valheim / "worlds_local" / "Midgard" / "Midgard.db").write_text("v1", encoding="utf-8")

    assert sync.main(["upload"]) == 0
    assert (env["cloud"] / "bucket" / "latest.tar.gz").is_file()

    assert sync.main(["remote", "add", "adminB", "bucketB/team-b"]) == 0
    assert sync.main(["switch", "adminB"]) == 0
    assert sync.main(["upload"]) == 0

    assert (env["cloud"] / "bucketB" / "team-b" / "latest.tar.gz").is_file()
    assert len(list((env["cloud"] / "bucket" / "history").iterdir())) == 1
    assert len(list((env["cloud"] / "bucketB" / "team-b" / "history").iterdir())) == 1
