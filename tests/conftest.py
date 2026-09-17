"""Shared fixtures: fake rclone (cloud on disk) + isolated config."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sync

FAKE_RCLONE = """#!/usr/bin/env python3
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["--config"]:
    args = args[2:]

cloud = Path(os.environ["VALHEIM_SYNC_FAKE_CLOUD"])


def local(remote: str) -> Path:
    _, _, path = remote.partition(":")
    return cloud / path


cmd = args[0]
if cmd == "copyto":
    first, second = args[1], args[2]
    if ":" in first and not Path(first).exists():
        source = local(first)
        Path(second).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, second)
    else:
        target = local(second)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(first, target)
elif cmd == "cat":
    path = local(args[1])
    if not path.exists():
        sys.exit(3)
    sys.stdout.write(path.read_text(encoding="utf-8"))
elif cmd == "lsf":
    path = local(args[1])
    if not path.exists():
        sys.exit(3)
    for child in sorted(path.iterdir()):
        print(child.name + ("/" if child.is_dir() else ""))
elif cmd == "deletefile":
    path = local(args[1])
    if path.exists():
        path.unlink()
elif cmd == "purge":
    path = local(args[1])
    if path.exists():
        shutil.rmtree(path)
else:
    sys.exit(2)
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("VALHEIM_SYNC_FAKE_CLOUD", str(tmp_path / "cloud"))
    monkeypatch.setattr(sync, "game_running", lambda: False)
    monkeypatch.setattr(sync, "rclone_conf_path", lambda: tmp_path / "rclone.conf")

    fake = tmp_path / "fake_rclone.py"
    fake.write_text(FAKE_RCLONE, encoding="utf-8")
    fake.chmod(0o755)

    def make_config(
        world: str = "Midgard",
        user: str = "Raja",
        history: int = 10,
        root: Path | None = None,
    ) -> Path:
        valheim = root or (tmp_path / f"valheim-{world}")
        (valheim / "worlds_local" / world).mkdir(parents=True, exist_ok=True)
        cfg = sync.Config(
            user=user,
            world=world,
            valheim_root=str(valheim),
            remote="valheim",
            remote_base=f"bucket/{world}",
            history_limit=history,
            rclone_bin=str(fake),
        )
        sync.save_config(cfg)
        return valheim

    return {
        "tmp": tmp_path,
        "make_config": make_config,
        "cloud": tmp_path / "cloud",
        "fake": fake,
    }
