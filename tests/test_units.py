"""Unit tests: helpers, detection, rclone install, init, CLI."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Self

import pytest

import sync


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data, self._data = self._data, b""
            return data
        data, self._data = self._data[:n], self._data[n:]
        return data


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_sanitize_and_human_size() -> None:
    assert sync.sanitize("My World/v2") == "My_World_v2"
    assert sync.sanitize("///") == "unknown"
    assert sync.human_size(0) == "0.0 B"
    assert sync.human_size(2048).startswith("2.0 KB")


def test_archive_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "World"
    (src / "sub").mkdir(parents=True)
    (src / "World.db").write_text("x", encoding="utf-8")
    (src / "sub" / "b").write_text("y", encoding="utf-8")

    archive = tmp_path / "world.tar.gz"
    sync.make_archive(src, archive)
    out = tmp_path / "out"
    out.mkdir()
    sync.extract_archive(archive, out)

    assert (out / "World" / "World.db").read_text(encoding="utf-8") == "x"
    assert (out / "World" / "sub" / "b").read_text(encoding="utf-8") == "y"


def test_extract_archive_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(sync.SyncError):
        sync.extract_archive(archive, tmp_path / "out")


def test_extract_archive_invalid(tmp_path: Path) -> None:
    bad = tmp_path / "bad.tar"
    bad.write_bytes(b"not a tar")
    with pytest.raises(sync.SyncError):
        sync.extract_archive(bad, tmp_path / "out")


def test_detect_valheim_root_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    target = (
        home
        / ".local/share/Steam/steamapps/compatdata/892970/pfx/drive_c"
        / "users/steamuser/AppData/LocalLow/IronGate/Valheim"
    )
    (target / "worlds_local").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert sync.detect_valheim_root() == target


def test_detect_valheim_root_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert sync.detect_valheim_root() is None


def test_resolve_valheim_root_explicit_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "vh"
    root.mkdir()
    assert sync.resolve_valheim_root(sync.Config(valheim_root=str(root))) == root
    with pytest.raises(sync.SyncError):
        sync.resolve_valheim_root(sync.Config(valheim_root=str(tmp_path / "nope")))


def test_install_rclone(env, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("rclone-current-linux-amd64/rclone", "#!/bin/sh\n")
    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda *a, **k: _FakeResp(buf.getvalue()))

    cfg = sync.Config(rclone_bin="auto")
    path = sync.install_rclone(cfg)
    assert Path(path).is_file()
    assert sync.load_config().rclone_bin == path


def test_run_rclone_failure(env) -> None:
    env["make_config"]()
    cfg = sync.load_config()
    with pytest.raises(sync.SyncError):
        sync.run_rclone(cfg, ["cat", sync.remote_uri(cfg, "meta.json")], capture=True)


def test_read_meta_invalid(env) -> None:
    env["make_config"]()
    cfg = sync.load_config()
    meta = env["cloud"] / "bucket" / "meta.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text("{invalid", encoding="utf-8")
    assert sync.read_meta(cfg) is None


def test_remote_available(env) -> None:
    env["make_config"]()
    cfg = sync.load_config()
    assert sync.remote_available(cfg) is False
    (env["cloud"] / "bucket").mkdir(parents=True, exist_ok=True)
    assert sync.remote_available(cfg) is True


def test_purge_history_missing_is_safe(env) -> None:
    env["make_config"]()
    cfg = sync.load_config()
    sync.purge_history(cfg)


def test_game_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **k: _Proc(stdout="valheim.exe\n"))
    assert sync.game_running() is True
    monkeypatch.setattr(
        sync.subprocess, "run", lambda *a, **k: _Proc(stdout="bash\npython3\nvalheim-sync\n")
    )
    assert sync.game_running() is False


def test_game_running_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise OSError("no pgrep")

    monkeypatch.setattr(sync.subprocess, "run", boom)
    assert sync.game_running() is False


def test_init_flow(env, monkeypatch: pytest.MonkeyPatch) -> None:
    valheim = env["tmp"] / "vh"
    (valheim / "worlds_local" / "Midgard").mkdir(parents=True)
    sync.save_config(sync.Config(rclone_bin=str(env["fake"])))

    answers = iter(["Tester", str(valheim), "valheim", "bucket", "kid", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(sync.getpass, "getpass", lambda *a: "appkey")

    assert sync.main(["init"]) == 0
    cfg = sync.load_config()
    assert cfg.user == "Tester"
    assert cfg.remote_base == "bucket"
    assert sync.rclone_conf_path().exists()


def test_set_path_and_cloud(env) -> None:
    env["make_config"]()
    new_root = env["tmp"] / "other"
    new_root.mkdir()
    assert sync.main(["set-path", str(new_root)]) == 0
    assert sync.load_config().valheim_root == str(new_root)

    assert sync.main(["set-cloud", "valheim:bucket/Other"]) == 0
    cfg = sync.load_config()
    assert cfg.remote == "valheim"
    assert cfg.remote_base == "bucket/Other"


def test_set_path_auto(env, monkeypatch: pytest.MonkeyPatch) -> None:
    env["make_config"]()
    monkeypatch.setattr(sync, "detect_valheim_root", lambda: None)
    assert sync.main(["set-path", "auto"]) == 0
    assert sync.load_config().valheim_root == "auto"


def test_set_cloud_invalid(env) -> None:
    env["make_config"]()
    assert sync.main(["set-cloud", "invalid"]) == 1


def test_upload_without_local_world(env) -> None:
    valheim = env["make_config"]()
    (valheim / "worlds_local").rmdir()
    assert sync.main(["upload"]) == 1


def test_upload_when_game_running(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "game_running", lambda: True)
    valheim = env["make_config"]()
    (valheim / "worlds_local" / "Midgard").mkdir()
    (valheim / "worlds_local" / "Midgard" / "Midgard.db").write_text("v1", encoding="utf-8")
    assert sync.main(["upload"]) == 1


def test_list_empty_history(env, capsys) -> None:
    env["make_config"]()
    assert sync.main(["list"]) == 0
    assert "no versions" in capsys.readouterr().out


def test_rclone_asset_unsupported_os(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.platform, "system", lambda: "plan9")
    with pytest.raises(sync.SyncError):
        sync._rclone_asset()


def test_rclone_asset_unsupported_arch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sync.platform, "machine", lambda: "mips")
    with pytest.raises(sync.SyncError):
        sync._rclone_asset()


def test_install_rclone_bad_zip(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda *a, **k: _FakeResp(b"nope"))
    with pytest.raises(sync.SyncError):
        sync.install_rclone(sync.Config(rclone_bin="auto"))


def test_load_config_corrupted(env) -> None:
    sync.config_path().write_text("{bad", encoding="utf-8")
    with pytest.raises(sync.SyncError):
        sync.load_config()


def test_read_meta_valid(env) -> None:
    env["make_config"]()
    cfg = sync.load_config()
    meta = env["cloud"] / "bucket" / "meta.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"sha256": "abc"}), encoding="utf-8")
    assert sync.read_meta(cfg)["sha256"] == "abc"


def test_set_user_prompt(env, monkeypatch: pytest.MonkeyPatch) -> None:
    env["make_config"]()
    monkeypatch.setattr("builtins.input", lambda *a: "NewName")
    assert sync.main(["set-user"]) == 0
    assert sync.load_config().user == "NewName"


def test_set_path_prompt(env, monkeypatch: pytest.MonkeyPatch) -> None:
    env["make_config"]()
    new_root = env["tmp"] / "prompt-root"
    new_root.mkdir()
    monkeypatch.setattr("builtins.input", lambda *a: str(new_root))
    assert sync.main(["set-path"]) == 0
    assert sync.load_config().valheim_root == str(new_root)


def test_write_rclone_remote_idempotent(env) -> None:
    cfg = sync.Config(remote="valheim")
    sync.write_rclone_remote(cfg, "kid", "key")
    first = sync.rclone_conf_path().read_text(encoding="utf-8")
    sync.write_rclone_remote(cfg, "kid", "key")
    assert sync.rclone_conf_path().read_text(encoding="utf-8") == first


def test_resolve_valheim_root_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "vh"
    (root / "worlds_local").mkdir(parents=True)
    monkeypatch.setattr(sync, "detect_valheim_root", lambda: root)
    assert sync.resolve_valheim_root(sync.Config(valheim_root="auto")) == root


def test_worlds_dir(tmp_path: Path) -> None:
    root = tmp_path / "vh"
    (root / "worlds_local").mkdir(parents=True)
    cfg = sync.Config(valheim_root=str(root))
    assert sync.worlds_dir(cfg) == root / "worlds_local"


def test_normalize_valheim_root(tmp_path: Path) -> None:
    root = tmp_path / "vh"
    worlds = root / "worlds_local"
    worlds.mkdir(parents=True)
    assert sync.normalize_valheim_root(worlds) == root
    assert sync.normalize_valheim_root(root) == root


def test_cloud_saves_dir(tmp_path: Path) -> None:
    root = tmp_path / "vh"
    (root / "worlds_local").mkdir(parents=True)
    cfg = sync.Config(valheim_root=str(root))
    assert sync.cloud_saves_dir(cfg) == root / "worlds"


def test_prompt_and_confirm_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_eof(*_a):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert sync.prompt("x", "defaut") == "defaut"
    assert sync.confirm("q") is False


def test_resolve_rclone_which(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "rclone"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sync.shutil, "which", lambda _name: str(exe))
    assert sync.resolve_rclone(sync.Config(rclone_bin="auto")) == str(exe)


def test_resolve_rclone_installs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda _name: None)
    monkeypatch.setattr(sync, "install_rclone", lambda _cfg: "/fake/rclone")
    assert sync.resolve_rclone(sync.Config(rclone_bin="auto")) == "/fake/rclone"


def test_remote_uri_parts() -> None:
    cfg = sync.Config(remote="r", remote_base="b/W")
    assert sync.remote_uri(cfg) == "r:b/W"
    assert sync.remote_uri(cfg, "meta.json") == "r:b/W/meta.json"


def test_human_size_go() -> None:
    assert sync.human_size(5 * 1024**3).endswith("GB")


def test_detect_valheim_root_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    root = home / "AppData" / "LocalLow" / "IronGate" / "Valheim"
    (root / "worlds_local").mkdir(parents=True)

    class _FakePath:
        @staticmethod
        def home() -> Path:
            return home

    monkeypatch.setattr(sync.os, "name", "nt")
    monkeypatch.setattr(sync, "Path", _FakePath)
    assert sync.detect_valheim_root() == root


def test_warn_about_steam_cloud(env, capsys) -> None:
    root = env["make_config"]()
    (root / "worlds").mkdir()
    (root / "worlds" / "Steam.db").write_text("x", encoding="utf-8")

    sync._warn_about_steam_cloud(sync.load_config())
    assert "Steam Cloud" in capsys.readouterr().err


def test_set_path_accepts_worlds_local(env) -> None:
    valheim = env["make_config"]()
    assert sync.main(["set-path", str(valheim / "worlds_local")]) == 0
    assert sync.load_config().valheim_root == str(valheim)
