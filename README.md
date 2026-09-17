# valheim-sync

Share one **Valheim world** with friends through free cloud storage, no dedicated server.
Flow: `download` → play → `upload`. Anyone can become the host.

Windows + Linux. Python 3.12+ (stdlib only). Cloud: **Backblaze B2** (10 GB free) via **rclone**.

---

## How it works

Valheim is P2P: only the host holds the world save. `valheim-sync` copies the world folder to
the cloud after your session and restores it before the next one. The cloud always keeps the
latest version plus a history (10 max) tagged with the uploader's name.

```
Player 1 (host) :  play    ->  upload
Player 2        :  download  ->  play  ->  upload
Player 3        :  download  ->  play  ->  upload
```

The world is stored as a `tar` archive (integrity checked with sha256).

---

## Requirements

- **Python 3.12+** ([python.org](https://www.python.org/downloads/), tick "Add to PATH" on Windows).
- **rclone**: *installed automatically* by the script when missing (Windows/Linux).
- One free **Backblaze B2 account** (created once by the group, not per player).

---

## Backblaze B2 setup (once)

1. Create an account at [backblaze.com](https://www.backblaze.com/sign-up/cloud-storage) (free tier: 10 GB).
2. **Buckets** → *Create a Bucket*:
   - name: `valheim-sync`
   - *Files in Bucket are*: **Private**
3. **Application Keys** → *Add a New Application Key*:
   - *Allow access to Bucket(s)*: `valheim-sync` only
   - note the **keyID** and **applicationKey** (shown once).
4. Pass them to `init` (below). The generated `rclone.conf` holds these keys:
   **never commit it** (already in `.gitignore`). Share it with friends privately.

---

## Install

```bash
git clone git@github.com:RajaRakoto/valheim-sync.git
cd valheim-sync
python sync.py init
```

`init` asks for: username, world name, Valheim folder (auto-detected), rclone remote, bucket,
then the B2 keys (only if `rclone.conf` does not exist yet).

---

## Usage

### First host

```bash
python sync.py init      # configure
# launch Valheim, create/play the world, quit the game
python sync.py upload    # push the world to the cloud
```

### Joining (new player)

```bash
python sync.py init      # same bucket, same world name
python sync.py download  # fetch the latest version
# play, quit
python sync.py upload
```

### Commands

| Command | Effect |
|---|---|
| `init` | configure username, world, path, cloud |
| `upload` | push the local world (refused while Valheim runs) |
| `download` | fetch the latest version + local backup |
| `status` | compare local vs cloud (date, uploader, state) |
| `list` | cloud history (10 max) |
| `set-user <name>` | change your username (tracked on upload) |
| `set-world <name>` | switch world (purges the old cloud copy) |
| `set-path <folder>` | force the Valheim folder (useful on Linux) |
| `set-cloud <remote:path>` | change the remote/bucket |

Examples:

```bash
python sync.py set-user Kratos
python sync.py set-world Asgard
python sync.py set-path "/home/raja/.local/share/Steam/steamapps/compatdata/892970/pfx/drive_c/users/steamuser/AppData/LocalLow/IronGate/Valheim"
```

---

## Locations

**Script's local files**

| File | Windows | Linux |
|---|---|---|
| config | `%APPDATA%\valheim-sync\config.json` | `~/.config/valheim-sync/config.json` |
| state | `%APPDATA%\valheim-sync\state.json` | `~/.config/valheim-sync/state.json` |
| rclone | `<repo>\rclone.conf` | `<repo>/rclone.conf` |

**Valheim saves** (auto-detected)

- Windows: `%USERPROFILE%\AppData\LocalLow\IronGate\Valheim\`
- Linux (Proton, app 892970):
  `~/.local/share/Steam/steamapps/compatdata/892970/pfx/drive_c/users/steamuser/AppData/LocalLow/IronGate/Valheim/`
  (also `~/.steam/steam/...` and Flatpak `~/.var/app/com.valvesoftware.Steam/...`)

The world lives in `worlds_local/<WorldName>/` (Valheim 1.0 format = folder).

---

## Safety

- **Valheim must be closed**: `upload`/`download` refuse while the game runs.
- **Automatic local backup** before every `download` (`<World>.bak-<date>`).
- **Integrity check**: sha256 of the archive before restoring.
- **Overwrite guard**: if the cloud changed since your last `download`, `upload` is refused
  (run `download` first). No stale locks.
- **B2 keys** in `rclone.conf`: sensitive file, do not publish.

---

## Development

```bash
uv run --with pytest --with pytest-cov pytest --cov=sync
uvx ruff check .
uvx ruff format .
```

Tests: 51, coverage ~92%. The tests' fake rclone simulates the cloud on disk.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Valheim folder not found` | `python sync.py set-path <folder>` |
| `rclone failed ... bucket` | check bucket/B2 keys, rerun `init` |
| `Conflict: the cloud changed` | `python sync.py download`, then replay/`upload` |
| Valheim does not see the restored world | check `set-path`, world format 1.0 |
| History too large | automatic, 10 versions max |

---

## License

MIT.
