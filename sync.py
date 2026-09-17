#!/usr/bin/env python3
"""valheim-sync - share Valheim local saves through free cloud storage.

Sync the whole worlds_local folder between several players (Windows/Linux)
using rclone and Backblaze B2, as a compressed tar.gz archive.
Flow: download -> play -> upload.

Requires Python 3.12+. No external dependency (stdlib only).
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

APP_NAME = "valheim-sync"
VERSION = "1.1.0"
VALHEIM_APPID = "892970"
RCLONE_DL = "https://downloads.rclone.org"
USER_AGENT = f"{APP_NAME}/{VERSION}"
HASH_CHUNK_SIZE = 1 << 20
RCLONE_DOWNLOAD_TIMEOUT = 120
RCLONE_CONF_MODE = 0o600
DEFAULT_REMOTE = "valheim"
DEFAULT_BUCKET = "valheim-sync"
WORLDS_DIRNAME = "worlds_local"
CLOUD_SAVES_DIRNAME = "worlds"
ARCHIVE_SUFFIX = ".tar.gz"
GZIP_COMPRESSLEVEL = 6


class SyncError(Exception):
    """Fatal user error."""


def log(msg: str) -> None:
    print(f"[{APP_NAME}] {msg}")


def warn(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr)


def die(msg: str) -> None:
    raise SyncError(msg)


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------


def project_dir() -> Path:
    return Path(__file__).resolve().parent


def rclone_conf_path() -> Path:
    return project_dir() / "rclone.conf"


def config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def state_path() -> Path:
    return config_dir() / "state.json"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


@dataclass
class Config:
    user: str = ""
    valheim_root: str = "auto"
    remote: str = "valheim"
    remote_base: str = ""
    history_limit: int = 10
    rclone_bin: str = "auto"


@dataclass
class State:
    base_sha: str = ""


def _load_json(path: Path, cls):
    if not path.exists():
        return cls()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"unreadable file {path}: {exc}")
    if not isinstance(data, dict):
        die(f"invalid file {path}: expected a JSON object")
    known = set(cls.__dataclass_fields__)
    return cls(**{k: v for k, v in data.items() if k in known})


def _save_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(obj), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_config() -> Config:
    return _load_json(config_path(), Config)


def save_config(cfg: Config) -> None:
    _save_json(config_path(), cfg)


def load_state() -> State:
    return _load_json(state_path(), State)


def save_state(state: State) -> None:
    _save_json(state_path(), state)


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"


def iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "unknown"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(num: int) -> str:
    size = float(num)
    unit = "B"
    for bigger in ("KB", "MB", "GB"):
        if size < 1024:
            break
        size /= 1024
        unit = bigger
    return f"{size:.1f} {unit}"


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


def prompt_secret(label: str) -> str:
    return getpass.getpass(f"{label}: ").strip()


def confirm(question: str) -> bool:
    try:
        return input(f"{question} (type YES): ").strip() == "YES"
    except EOFError:
        return False


# --------------------------------------------------------------------------
# Valheim detection
# --------------------------------------------------------------------------


def _steam_roots() -> list[Path]:
    home = Path.home()
    return [
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".steam/root",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        home / ".var/app/com.valvesoftware.Steam/.steam/steam",
    ]


def _libraries_from_vdf(vdf: Path) -> list[Path]:
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return [
        path
        for match in re.finditer(r'"path"\s+"([^"]+)"', text)
        if (path := Path(match.group(1).replace("\\\\", "/"))).exists()
    ]


def _steam_libraries() -> list[Path]:
    libraries: list[Path] = []
    for root in _steam_roots():
        if root.exists():
            libraries.append(root)
        libraries.extend(_libraries_from_vdf(root / "steamapps" / "libraryfolders.vdf"))
    return libraries


def _valheim_root_in(library: Path) -> Path:
    return (
        library
        / "steamapps"
        / "compatdata"
        / VALHEIM_APPID
        / "pfx"
        / "drive_c"
        / "users"
        / "steamuser"
        / "AppData"
        / "LocalLow"
        / "IronGate"
        / "Valheim"
    )


def _linux_candidates() -> list[Path]:
    return [_valheim_root_in(library) for library in _steam_libraries()]


def detect_valheim_root() -> Path | None:
    if os.name == "nt":
        candidates = [Path.home() / "AppData" / "LocalLow" / "IronGate" / "Valheim"]
    else:
        candidates = _linux_candidates()
    existing = [c for c in candidates if c.exists()]
    for c in existing:
        if (c / "worlds_local").exists():
            return c
    return existing[0] if existing else None


def normalize_valheim_root(path: Path) -> Path:
    """Accept either the Valheim root or the worlds_local folder itself."""
    if path.name == WORLDS_DIRNAME:
        return path.parent
    return path


def resolve_valheim_root(cfg: Config) -> Path:
    if cfg.valheim_root and cfg.valheim_root != "auto":
        root = normalize_valheim_root(Path(cfg.valheim_root).expanduser())
        if not root.exists():
            die(f"Valheim folder not found: {root} (fix with `set-path`)")
        return root
    detected = detect_valheim_root()
    if detected is None:
        die(
            "Valheim folder not found automatically. "
            f"Use `set-path <folder>` (the parent of {WORLDS_DIRNAME})."
        )
    return detected


def worlds_dir(cfg: Config) -> Path:
    return resolve_valheim_root(cfg) / WORLDS_DIRNAME


def cloud_saves_dir(cfg: Config) -> Path:
    return resolve_valheim_root(cfg) / CLOUD_SAVES_DIRNAME


def game_running() -> bool:
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist"],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            ).stdout.lower()
            return "valheim" in out
        out = subprocess.run(
            ["ps", "-A", "-o", "comm="],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        ).stdout.lower()
        return any(line.strip().startswith("valheim.") for line in out.splitlines())
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_game_closed() -> None:
    if game_running():
        die("Valheim is running. Close the game before continuing.")


# --------------------------------------------------------------------------
# rclone
# --------------------------------------------------------------------------


def _rclone_asset() -> tuple[str, str]:
    system = platform.system().lower()
    os_name = {"windows": "windows", "linux": "linux", "darwin": "osx"}.get(system)
    if os_name is None:
        die(f"unsupported OS for rclone: {system}")
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "arm",
        "armv6l": "arm",
        "i386": "386",
        "i686": "386",
    }.get(machine)
    if arch is None:
        die(f"unsupported architecture for rclone: {machine}")
    return os_name, arch


def install_rclone(cfg: Config) -> str:
    os_name, arch = _rclone_asset()
    url = f"{RCLONE_DL}/rclone-current-{os_name}-{arch}.zip"
    bin_dir = config_dir() / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log(f"rclone missing — downloading ({os_name}/{arch})...")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "rclone.zip"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with (
                urllib.request.urlopen(request, timeout=RCLONE_DOWNLOAD_TIMEOUT) as resp,
                archive.open("wb") as fh,
            ):
                shutil.copyfileobj(resp, fh)
        except OSError as exc:
            die(f"rclone download failed: {exc}")
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp)  # trusted source: rclone's official zip
        except zipfile.BadZipFile as exc:
            die(f"invalid rclone archive: {exc}")
        exe_name = "rclone.exe" if os.name == "nt" else "rclone"
        found = next(Path(tmp).rglob(exe_name), None)
        if found is None:
            die("rclone binary not found in the archive")
        dest = bin_dir / exe_name
        shutil.copy2(found, dest)
        dest.chmod(0o755)
    cfg.rclone_bin = str(dest)
    save_config(cfg)
    log(f"rclone installed: {dest}")
    return str(dest)


def resolve_rclone(cfg: Config) -> str:
    if cfg.rclone_bin and cfg.rclone_bin != "auto" and Path(cfg.rclone_bin).exists():
        return cfg.rclone_bin
    found = shutil.which("rclone")
    if found:
        return found
    return install_rclone(cfg)


def run_rclone(
    cfg: Config, args: list[str], *, capture: bool = False, check: bool = True
) -> subprocess.CompletedProcess[str]:
    binary = resolve_rclone(cfg)
    cmd = [binary, "--config", str(rclone_conf_path()), *args]
    result = subprocess.run(cmd, capture_output=capture, text=True, errors="replace", check=False)
    if check and result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        die(f"rclone failed ({' '.join(args)})" + (f": {detail}" if detail else ""))
    return result


def remote_uri(cfg: Config, *parts: str) -> str:
    base = f"{cfg.remote}:{cfg.remote_base.strip('/')}"
    if parts:
        return base + "/" + "/".join(p.strip("/") for p in parts)
    return base


def remote_section_exists(cfg: Config) -> bool:
    conf = rclone_conf_path()
    if not conf.exists():
        return False
    text = conf.read_text(encoding="utf-8", errors="ignore")
    return re.search(rf"^\[{re.escape(cfg.remote)}\]\s*$", text, re.MULTILINE) is not None


def write_rclone_remote(cfg: Config, account: str, key: str) -> None:
    if remote_section_exists(cfg):
        log("rclone remote already present, kept as is")
        return
    conf = rclone_conf_path()
    block = f"[{cfg.remote}]\ntype = b2\naccount = {account}\nkey = {key}\n"
    with conf.open("a", encoding="utf-8") as fh:
        if conf.stat().st_size:
            fh.write("\n")
        fh.write(block)
    with suppress(OSError):
        conf.chmod(RCLONE_CONF_MODE)


def remote_available(cfg: Config) -> bool:
    bucket = cfg.remote_base.split("/", 1)[0] if cfg.remote_base else ""
    target = f"{cfg.remote}:{bucket}" if bucket else remote_uri(cfg)
    result = run_rclone(cfg, ["lsf", target], capture=True, check=False)
    return result.returncode == 0


def read_meta(cfg: Config) -> dict | None:
    result = run_rclone(cfg, ["cat", remote_uri(cfg, "meta.json")], capture=True, check=False)
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        meta = json.loads(result.stdout)
    except json.JSONDecodeError:
        warn("cloud meta.json unreadable")
        return None
    if not isinstance(meta, dict):
        warn("cloud meta.json invalid (expected a JSON object)")
        return None
    return meta


def upload_text(cfg: Config, name: str, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        tmp = Path(fh.name)
    try:
        run_rclone(cfg, ["copyto", str(tmp), remote_uri(cfg, name)])
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Archives
# --------------------------------------------------------------------------


def make_archive(src: Path, out: Path) -> None:
    with tarfile.open(out, "w:gz", compresslevel=GZIP_COMPRESSLEVEL) as tar_file:
        tar_file.add(src, arcname=src.name)


def extract_archive(archive: Path, dest: Path) -> None:
    try:
        with tarfile.open(archive, "r:*") as tar_file:
            tar_file.extractall(dest, filter="data")
    except tarfile.TarError as exc:
        die(f"invalid archive: {exc}")


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def require_ready() -> Config:
    if not config_path().exists():
        die("no configuration. Run `init` first.")
    cfg = load_config()
    missing = [name for name in ("user", "remote_base") if not getattr(cfg, name)]
    if missing:
        die(f"incomplete configuration ({', '.join(missing)}). Run `init` again.")
    return cfg


def list_history(cfg: Config) -> list[str]:
    result = run_rclone(cfg, ["lsf", remote_uri(cfg, "history")], capture=True, check=False)
    if result.returncode != 0:
        return []
    return sorted(
        (line.strip() for line in (result.stdout or "").splitlines() if line.strip()),
        reverse=True,
    )


def purge_history(cfg: Config) -> None:
    limit = max(int(cfg.history_limit), 1)
    for stale in list_history(cfg)[limit:]:
        run_rclone(cfg, ["deletefile", remote_uri(cfg, "history", stale)], check=False)
        log(f"history purged: {stale}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _collect_identity(cfg: Config) -> None:
    cfg.user = prompt("Username", cfg.user or getpass.getuser())
    if not cfg.user:
        die("username required")


def _collect_valheim_root(cfg: Config) -> None:
    detected = detect_valheim_root()
    default_root = (
        cfg.valheim_root if cfg.valheim_root != "auto" else (str(detected) if detected else "")
    )
    root = prompt(f"Valheim folder (parent of {WORLDS_DIRNAME})", default_root)
    if not root:
        die("Valheim folder required")
    resolved = normalize_valheim_root(Path(root).expanduser())
    if not resolved.exists():
        die(f"folder not found: {resolved}")
    cfg.valheim_root = str(resolved)


def _collect_cloud(cfg: Config) -> None:
    cfg.remote = prompt("rclone remote name", cfg.remote or DEFAULT_REMOTE)
    cfg.remote_base = prompt("Backblaze B2 bucket", DEFAULT_BUCKET)


def _ensure_rclone_remote(cfg: Config) -> None:
    if remote_section_exists(cfg):
        log("rclone remote already configured")
        return
    log("Backblaze B2 credentials (Application Key)")
    account = prompt("keyID", "")
    if not account:
        die("keyID required")
    key = prompt_secret("applicationKey")
    if not key:
        die("applicationKey required")
    write_rclone_remote(cfg, account, key)


def _warn_about_steam_cloud(cfg: Config) -> None:
    cloud_saves = cloud_saves_dir(cfg)
    if cloud_saves.is_dir() and any(cloud_saves.iterdir()):
        warn(
            f"Steam Cloud saves detected in {cloud_saves}. "
            f"valheim-sync only syncs local saves ({WORLDS_DIRNAME})."
        )
    log("Reminder: use LOCAL saves for Valheim (disable Steam Cloud).")
    log("In Valheim: world list > Manage saves to migrate cloud -> local.")


def cmd_init(args: argparse.Namespace) -> None:
    cfg = load_config()
    log(f"Configuration {APP_NAME} v{VERSION}")
    _collect_identity(cfg)
    _collect_valheim_root(cfg)
    _collect_cloud(cfg)
    _ensure_rclone_remote(cfg)
    save_config(cfg)

    if remote_available(cfg):
        log("cloud access OK")
    else:
        warn("cloud access unavailable for now (check bucket/keys)")

    _warn_about_steam_cloud(cfg)

    worlds_path = worlds_dir(cfg)
    if worlds_path.is_dir():
        log(f"local worlds detected: {worlds_path}")
        if confirm("Upload these worlds now?"):
            cmd_upload(args)
    else:
        log(f"local worlds missing ({worlds_path})")
        log("To join an existing save: `download`.")
        log("Otherwise play Valheim (local saves) then `upload`.")


def cmd_upload(args: argparse.Namespace) -> None:
    cfg = require_ready()
    ensure_game_closed()
    worlds_path = worlds_dir(cfg)
    if not worlds_path.is_dir():
        die(f"local worlds not found: {worlds_path}")

    meta = read_meta(cfg)
    remote_sha = (meta or {}).get("sha256", "")
    state = load_state()
    if remote_sha and remote_sha != state.base_sha:
        die("Conflict: the cloud changed since your last download. Run `download` first.")

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / f"{WORLDS_DIRNAME}{ARCHIVE_SUFFIX}"
        log(f"creating the archive of {WORLDS_DIRNAME}...")
        make_archive(worlds_path, archive_path)
        sha = sha256_file(archive_path)
        size = archive_path.stat().st_size
        log(f"archive: {human_size(size)} sha256={sha[:12]}")

        run_rclone(cfg, ["copyto", str(archive_path), remote_uri(cfg, f"latest{ARCHIVE_SUFFIX}")])
        history_name = f"{utc_stamp()}_{sanitize(cfg.user)}{ARCHIVE_SUFFIX}"
        run_rclone(cfg, ["copyto", str(archive_path), remote_uri(cfg, "history", history_name)])
        log(f"history added: {history_name}")

    new_meta = {
        "uploader": cfg.user,
        "timestamp": iso_now(),
        "sha256": sha,
        "base_sha": remote_sha,
        "size": size,
    }
    upload_text(cfg, "meta.json", json.dumps(new_meta, indent=2, ensure_ascii=False))

    purge_history(cfg)

    state.base_sha = sha
    save_state(state)
    log(f"upload complete (by {cfg.user})")


def cmd_download(args: argparse.Namespace) -> None:
    cfg = require_ready()
    meta = read_meta(cfg)
    if not meta:
        die("no cloud backup. A host must `upload` first.")
    remote_sha = meta.get("sha256", "")
    if not remote_sha:
        die("invalid cloud meta.json (missing sha256)")

    worlds_path = worlds_dir(cfg)
    worlds_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / f"latest{ARCHIVE_SUFFIX}"
        log(
            "downloading the worlds "
            f"(by {meta.get('uploader', '?')}, {meta.get('timestamp', '?')})..."
        )
        run_rclone(cfg, ["copyto", remote_uri(cfg, f"latest{ARCHIVE_SUFFIX}"), str(archive_path)])

        got = sha256_file(archive_path)
        if got != remote_sha:
            die("invalid integrity (sha256 mismatch). No local change made.")

        extract_dir = Path(tmp) / "x"
        extract_dir.mkdir()
        extract_archive(archive_path, extract_dir)

        inner = extract_dir / WORLDS_DIRNAME
        if not inner.is_dir():
            dirs = [p for p in extract_dir.iterdir() if p.is_dir()]
            if len(dirs) == 1:
                inner = dirs[0]
            else:
                die("unexpected archive content")

        if worlds_path.exists():
            backup = worlds_path.with_name(f"{worlds_path.name}.bak-{utc_stamp()}")
            worlds_path.rename(backup)
            log(f"local backup: {backup.name}")

        shutil.move(str(inner), str(worlds_path))

    state = load_state()
    state.base_sha = remote_sha
    save_state(state)
    log(f"download complete (base={remote_sha[:12]})")


def cmd_status(args: argparse.Namespace) -> None:
    cfg = require_ready()
    worlds_path = worlds_dir(cfg)
    state = load_state()
    meta = read_meta(cfg)

    world_count = sum(1 for _ in worlds_path.iterdir()) if worlds_path.is_dir() else 0
    print(f"user          : {cfg.user}")
    print(f"worlds folder : {worlds_path} {'[present]' if worlds_path.is_dir() else '[absent]'}")
    print(f"local worlds  : {world_count}")
    if meta:
        print(f"cloud uploader: {meta.get('uploader', '?')}")
        print(f"cloud date    : {meta.get('timestamp', '?')}")
        print(f"cloud size    : {human_size(int(meta.get('size', 0)))}")
        print(f"cloud sha     : {str(meta.get('sha256', ''))[:12]}")
        if not state.base_sha:
            print("state         : never synced -> download")
        elif state.base_sha == meta.get("sha256"):
            print("state         : up to date")
        else:
            print("state         : behind / conflict -> download")
    else:
        print("cloud         : empty (no backup)")


def cmd_list(args: argparse.Namespace) -> None:
    cfg = require_ready()
    entries = list_history(cfg)
    if not entries:
        log("no versions in history")
        return
    print(f"History ({len(entries)}/{cfg.history_limit}):")
    for index, name in enumerate(entries, start=1):
        base = name.removesuffix(ARCHIVE_SUFFIX)
        stamp, _, uploader = base.partition("_")
        print(f"  {index:>2}. {stamp}  by {uploader or '?'}")


def cmd_set_user(args: argparse.Namespace) -> None:
    cfg = require_ready()
    new_user = args.name or prompt("New username", cfg.user)
    if not new_user:
        die("username required")
    cfg.user = new_user
    save_config(cfg)
    log(f"username set: {new_user}")


def cmd_set_path(args: argparse.Namespace) -> None:
    cfg = require_ready()
    new_path = args.path or prompt(f"Valheim folder (parent of {WORLDS_DIRNAME})", cfg.valheim_root)
    if not new_path or new_path == "auto":
        cfg.valheim_root = "auto"
        save_config(cfg)
        detected = detect_valheim_root()
        log(f"auto detection: {detected or 'failed'}")
        return
    resolved = normalize_valheim_root(Path(new_path).expanduser())
    if not resolved.exists():
        die(f"folder not found: {resolved}")
    cfg.valheim_root = str(resolved)
    save_config(cfg)
    log(f"Valheim path set: {resolved}")


def cmd_set_cloud(args: argparse.Namespace) -> None:
    cfg = require_ready()
    value = args.value or prompt("remote:base (e.g. valheim:valheim-sync)", "")
    if ":" not in value:
        die("expected format remote:path")
    remote, _, base = value.partition(":")
    if not remote or not base:
        die("expected format remote:path")
    cfg.remote = remote
    cfg.remote_base = base
    save_config(cfg)
    log(f"cloud set: {cfg.remote}:{cfg.remote_base}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Share Valheim local saves via cloud (download -> play -> upload).",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="configure the script (username, B2, paths)")
    sub.add_parser("upload", help="push local saves (worlds_local) to the cloud")
    sub.add_parser("download", help="fetch the latest saves into worlds_local")
    sub.add_parser("status", help="compare local and cloud")
    sub.add_parser("list", help="list the cloud history")

    p_user = sub.add_parser("set-user", help="change your username")
    p_user.add_argument("name", nargs="?")

    p_path = sub.add_parser("set-path", help="set the Valheim folder")
    p_path.add_argument("path", nargs="?")

    p_cloud = sub.add_parser("set-cloud", help="set the cloud remote (remote:path)")
    p_cloud.add_argument("value", nargs="?")

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except OSError:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "init": cmd_init,
        "upload": cmd_upload,
        "download": cmd_download,
        "status": cmd_status,
        "list": cmd_list,
        "set-user": cmd_set_user,
        "set-path": cmd_set_path,
        "set-cloud": cmd_set_cloud,
    }

    try:
        handlers[args.command](args)
    except SyncError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[cancelled]", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
