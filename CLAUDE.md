# CLAUDE.md

GOG Backup Installer for Steam Deck: a fullscreen, gamepad-first Tk app that lists GOG offline installers on an
SMB guest share or a USB drive / SD card, runs the chosen `setup_*.exe` under Proton straight from that source
(no copy to the Deck), and adds the installed games to Steam as non-Steam shortcuts.

## Files

- `gog_installer.py`: the whole app, one file. Sections are marked `# --- name ---`, in this order: config and
  state, SMB guest shares (`SmbFs`), USB drives, sources, installer parsing and title lookup, GOG's public data,
  running an installer (`InstallJob`), Steam `shortcuts.vdf`, artwork, per-game fixes and ProtonDB, GOG's
  DDrawCompat patch, gamepad (`Gamepad`), UI (cover helpers, then `App`), `main()`.
- The UI is one Tk canvas laid out at 1280×800 and scaled; `draw_<screen>` draws each screen, `self.hits`
  holds tap targets, and `action_a/b/x/y/menu/view` and `move(dx, dy)` handle the gamepad. The library is
  a cover grid (`library_entries`, `entries`, `view`); installed games without a source still show.
- `test_gog_installer.py`: unit tests for everything that touches Steam's files or decides what gets installed.
- `gog-installer.sh`: what the Steam shortcut starts. `install.sh`: copies the app and fonts to the Deck over SSH.
- `cover.svg`, `wide.svg`: the app's own Steam artwork. `fonts/`: Barlow Semi Condensed (OFL).

## Commands

```bash
python3 -m unittest test_gog_installer          # all tests; run after every change
./install.sh [user@host]                         # copy to the Deck (default deck@steamdeck.local)
```

On the Deck: `gog_installer.py --list` prints what each source holds; `--apply-steam`, `--fetch-art` and
`--update-protondb [--force]` are the other entry points (see the module docstring). `GOGI_WINDOWED=1` runs the
UI in a 1280×800 window instead of fullscreen. The app itself only runs on SteamOS: it needs evdev, libsmbclient,
libfuse 2, udisks, rsvg-convert and Steam's Proton, so on a Mac only the tests run. Don't open Tk with macOS's
own `/usr/bin/python3`: its Tk 8.5 crashes on current macOS.

## Rules

- **Standard library only, one file.** SteamOS's read-only root ships python3 with tkinter and evdev and nothing
  else to install. No pip packages, no new modules. Native libraries go through `ctypes` (see `SmbFs`).
- **Sources are read-only.** SMB is mounted read-only through our own FUSE layer (`--smbfs`, libsmbclient guest
  access, no root, no fstab); USB via udisks. Never write to a source, never copy or cache installers.
- **Ids must stay stable.** `game_id()` comes from the setup file name without version, and `shortcut_appid()`
  is a CRC of the slug; they decide the compatdata prefix (saves) and the Steam shortcut. Changing either
  orphans existing installs.
- **Old state and config keep working.** `migrate_config()` and `installed_slug()` read every older format;
  extend them instead of breaking them, and add a test for the old shape.
- **Steam's files are written only while Steam is stopped**, by `apply_steam()` running in its own
  `systemd-run --user` unit (stopping Steam kills the app). Back up each file as `*.gogi.bak`, write to `.tmp`,
  then `os.replace`. Steam must always be restarted, even after an error.
- **Fixes are opt-in.** ProtonDB suggestions are only shown; nothing is applied unless the player turns it on.
  Launch options pass through `SAFE_ENV` / `SAFE_ARG`. The DDrawCompat patch keeps the original exe as `*.gog`.
- **Covers:** Tk reads only PNG/GIF and can't scale, so `cover_thumbnail` wraps a JPEG/PNG in an SVG and
  runs `rsvg-convert` (shipped with SteamOS) to a PNG at the exact size, cached under `COVERS`. Covers load
  on one worker thread and reach the UI as `("cover", …)` events; never touch Tk off the UI thread.
- **Play** goes through Steam (`play_url`: the shortcut's app id << 32 | 0x02000000), never by running the exe.
- **ctypes layouts assume x86_64 glibc** (`STAT_SIZE`, `DIRENT_NAME`, `struct fuse_operations` of libfuse 2.9).
- **Network lookups** (GOG catalogue, gogdb.org, Steam store, ProtonDB export) are cached in `DATA`; tests must
  not hit the network: patch `http_get`, `catalog_match`, `gogdb_match`, `steam_match` etc. with
  `mock.patch.object(g, ...)`, and point `STEAM` and the cache files at a temp dir, as the existing tests do.

## On the Deck

- App: `~/.local/share/gog-installer/`. Data (`DATA`): the same folder: `state.json`, `install.log`,
  `apply-steam.log`, lookup and ProtonDB caches, per-game `logs/`.
- Config: `~/.config/gog-installer/config.json` (`{"sources": [...], "proton": "<default for new installs>"}`).
  Mounts and covers: `~/.cache/gog-installer/`.
- Installers run silently with Inno Setup flags (`SILENT_FLAGS`) in `steamapps/compatdata/<shortcut appid>`.

## Style

- Comments and docstrings say what something is or why, in plain short sentences, often with a real example
  (`"setup_hollow_knight_1.5.12620_(64bit)_(89718).exe -> "hollow_knight""`).
- UI copy and README are short and gamepad-first: name the button, then what it does. Colours are the purple
  constants at the top of the UI section.
