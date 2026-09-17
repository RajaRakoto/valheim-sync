# valheim-sync

Share your Valheim **local saves** with friends through free cloud storage, no dedicated server.
Flow: `download` → play → `upload`. Anyone can become the host.

The whole `worlds_local` folder is synced (all your worlds), as a compressed `tar.gz` archive.
Windows + Linux. Python 3.12+ (stdlib only). Cloud: **Backblaze B2** (10 GB free) via **rclone**.

---

## How it works

Valheim is P2P: only the host holds the world save. `valheim-sync` archives the `worlds_local`
folder to the cloud after your session and restores it before the next one. The cloud always
keeps the latest version plus a history (10 max) tagged with the uploader's name; older archives
are purged automatically.

```
Player 1 (host) :  play    ->  upload
Player 2        :  download  ->  play  ->  upload
Player 3        :  download  ->  play  ->  upload
```

Only `worlds_local` matters. The `worlds` folder (Steam Cloud saves) is **never** touched.

---

## Local saves only (important)

Valheim can store saves either **locally** (`worlds_local`) or on **Steam Cloud** (`worlds`).
`valheim-sync` only syncs `worlds_local`, so you must play with local saves:

1. In Steam, open **Valheim → Properties → General** and **disable Steam Cloud**.
2. In Valheim, if a world lives in the cloud, migrate it once: **world list → Manage saves**,
   then move it to local.
3. Repeat for every player in the group.

`init` reminds you and warns when it detects a non-empty `worlds` folder.

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

`init` asks for: username, Valheim folder (auto-detected), rclone remote, bucket, then the B2
keys (only if `rclone.conf` does not exist yet).

---

## Usage

### First host

```bash
python sync.py init      # configure
# launch Valheim (local saves), create/play, quit the game
python sync.py upload    # push worlds_local to the cloud
```

### Joining (new player)

```bash
python sync.py init      # same bucket
python sync.py download  # fetch the latest saves
# play, quit
python sync.py upload
```

### Commands

| Command | Effect |
|---|---|
| `init` | configure username, path, cloud |
| `upload` | push local saves (`worlds_local`) (refused while Valheim runs) |
| `download` | fetch the latest saves + local backup |
| `status` | compare local vs cloud (date, uploader, state) |
| `list` | cloud history (10 max) |
| `set-user <name>` | change your username (tracked on upload) |
| `set-path <folder>` | set the Valheim folder (useful on Linux) |
| `set-cloud <remote:path>` | change the remote/bucket |

Examples:

```bash
python sync.py set-user Kratos
python sync.py set-path "/home/raja/GAMES/SteamLibrary/steamapps/compatdata/892970/pfx/drive_c/users/steamuser/AppData/LocalLow/IronGate/Valheim"
```

`set-path` accepts either the Valheim root or the `worlds_local` folder itself.

---

## Locations

**Script's local files**

| File | Windows | Linux |
|---|---|---|
| config | `%APPDATA%\valheim-sync\config.json` | `~/.config/valheim-sync/config.json` |
| state | `%APPDATA%\valheim-sync\state.json` | `~/.config/valheim-sync/state.json` |
| rclone | `<repo>\rclone.conf` | `<repo>/rclone.conf` |

**Valheim saves**

- Windows: `%USERPROFILE%\AppData\LocalLow\IronGate\Valheim\worlds_local\`
- Linux (Proton, app 892970), inside the Steam library used for the game:
  `<SteamLibrary>/steamapps/compatdata/892970/pfx/drive_c/users/steamuser/AppData/LocalLow/IronGate/Valheim/worlds_local/`

The Steam library path varies per user (e.g. `~/.local/share/Steam`, a custom
`~/GAMES/SteamLibrary`, or a Flatpak install). If auto-detection fails, pass the absolute path
with `set-path`.

---

## Safety

- **Valheim must be closed**: `upload`/`download` refuse while the game runs.
- **Automatic local backup** before every `download` (`worlds_local.bak-<date>`).
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

Tests: 54, coverage ~93%. The tests' fake rclone simulates the cloud on disk.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Valheim folder not found` | `python sync.py set-path <folder>` |
| `rclone failed ... bucket` | check bucket/B2 keys, rerun `init` |
| `Conflict: the cloud changed` | `python sync.py download`, then replay/`upload` |
| Valheim does not see the restored world | check `set-path`, use local saves |
| Steam Cloud warning on `init` | disable Steam Cloud, migrate to local saves |
| History too large | automatic, 10 versions max |

---

## License

MIT.
