"""End-to-end tests for valheim-sync (upload/download/status/list flows)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sync


def _write_world(valheim: Path, world: str, payload: str) -> None:
    world_dir = valheim / "worlds_local" / world
    (world_dir / "chunks").mkdir(parents=True, exist_ok=True)
    (world_dir / f"{world}.db").write_text(payload, encoding="utf-8")
    (world_dir / "chunks" / "part0").write_text(payload + "-chunk", encoding="utf-8")


def test_upload_creates_cloud(env) -> None:
    valheim = env["make_config"]()
    _write_world(valheim, "Midgard", "v1")

    assert sync.main(["upload"]) == 0

    base = env["cloud"] / "bucket" / "Midgard"
    assert (base / "latest.tar").is_file()
    meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
    assert meta["uploader"] == "Raja"
    assert meta["world"] == "Midgard"
    assert len(list((base / "history").iterdir())) == 1


def test_download_roundtrip(env) -> None:
    valheim_a = env["make_config"]()
    _write_world(valheim_a, "Midgard", "payload-abc")
    assert sync.main(["upload"]) == 0

    valheim_b = env["tmp"] / "valheim-b"
    env["make_config"](root=valheim_b)
    (valheim_b / "worlds_local" / "Midgard").rmdir()

    assert sync.main(["download"]) == 0
    restored = valheim_b / "worlds_local" / "Midgard"
    assert (restored / "Midgard.db").read_text(encoding="utf-8") == "payload-abc"
    assert (restored / "chunks" / "part0").read_text(encoding="utf-8") == "payload-abc-chunk"

    state = sync.load_state()
    meta = json.loads(
        (env["cloud"] / "bucket" / "Midgard" / "meta.json").read_text(encoding="utf-8")
    )
    assert state.base_sha == meta["sha256"]


def test_download_backs_up_existing_world(env) -> None:
    valheim = env["make_config"]()
    _write_world(valheim, "Midgard", "cloud-version")
    assert sync.main(["upload"]) == 0

    _write_world(valheim, "Midgard", "local-dirty")
    assert sync.main(["download"]) == 0

    backups = list((valheim / "worlds_local").glob("Midgard.bak-*"))
    assert len(backups) == 1
    assert (backups[0] / "Midgard.db").read_text(encoding="utf-8") == "local-dirty"


def test_conflict_blocks_upload(env) -> None:
    valheim = env["make_config"]()
    _write_world(valheim, "Midgard", "v1")
    assert sync.main(["upload"]) == 0
    assert sync.main(["download"]) == 0

    meta_path = env["cloud"] / "bucket" / "Midgard" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["sha256"] = "deadbeef"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert sync.main(["upload"]) == 1


def test_purge_history_limit(env) -> None:
    valheim = env["make_config"](history=3)
    for i in range(5):
        _write_world(valheim, "Midgard", f"v{i}")
        assert sync.main(["upload"]) == 0

    history = env["cloud"] / "bucket" / "Midgard" / "history"
    assert len(list(history.iterdir())) == 3


def test_download_rejects_corruption(env) -> None:
    valheim = env["make_config"]()
    _write_world(valheim, "Midgard", "v1")
    assert sync.main(["upload"]) == 0

    (env["cloud"] / "bucket" / "Midgard" / "latest.tar").write_bytes(b"corrupted")

    valheim_b = env["tmp"] / "valheim-b"
    env["make_config"](root=valheim_b)
    assert sync.main(["download"]) == 1


def test_download_without_cloud_fails(env) -> None:
    env["make_config"]()
    assert sync.main(["download"]) == 1


def test_set_world_purges_old_cloud(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "confirm", lambda _question: True)
    valheim = env["make_config"]()
    _write_world(valheim, "Midgard", "v1")
    assert sync.main(["upload"]) == 0

    assert sync.main(["set-world", "Asgard"]) == 0

    assert not (env["cloud"] / "bucket" / "Midgard").exists()
    cfg = sync.load_config()
    assert cfg.world == "Asgard"
    assert cfg.remote_base == "bucket/Asgard"
    assert sync.load_state().base_sha == ""


def test_set_world_cancelled(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "confirm", lambda _question: False)
    env["make_config"]()
    assert sync.main(["set-world", "Asgard"]) == 0
    assert sync.load_config().world == "Midgard"


def test_set_user_and_status(env, capsys) -> None:
    valheim = env["make_config"]()
    _write_world(valheim, "Midgard", "v1")
    assert sync.main(["upload"]) == 0
    assert sync.main(["set-user", "Kratos"]) == 0
    assert sync.load_config().user == "Kratos"

    assert sync.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Midgard" in out
    assert "Kratos" in out


def test_status_empty_cloud(env, capsys) -> None:
    env["make_config"]()
    assert sync.main(["status"]) == 0
    assert "empty" in capsys.readouterr().out


def test_list_shows_history(env, capsys) -> None:
    valheim = env["make_config"]()
    for i in range(2):
        _write_world(valheim, "Midgard", f"v{i}")
        assert sync.main(["upload"]) == 0

    assert sync.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "History" in out
    assert "Raja" in out


def test_require_ready_without_config(env) -> None:
    assert sync.main(["status"]) == 1
