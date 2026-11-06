# GOG Installer

A fullscreen, gamepad-first Steam Deck app. It lists GOG installers on a server folder, runs the one
you pick under Proton without copying it to the Deck, and adds finished games to Steam.

It needs only what SteamOS ships: Python with `tkinter` and `evdev`, `sshfs`, and Steam's Proton.

## Install

```bash
./install.sh                      # deck@steamdeck.local
```

Add the app itself to Steam (this restarts Steam, so close any game first):

```bash
ssh deck@steamdeck.local 'systemd-run --user --collect python3 ~/.local/share/gog-installer/gog_installer.py --apply-steam'
```

## Server access

The Deck reads installers over SFTP with its own key, `~/.ssh/id_drm_server`. On the server, that key is
limited to read-only file access in `authorized_keys`:

```text
restrict,command="internal-sftp -R" ssh-ed25519 AAAA... deck-rom-manager@steamdeck
```

Defaults are at the top of `gog_installer.py`. Override them in `~/.config/gog-installer/config.json`
with any of `host`, `path` and `key`. Keep one folder per game, with `setup_*.exe` and any `.bin` parts
directly inside it. Other folders inside it, such as `Extras`, are ignored.

## SMB shares

Press **Start** in the app for Settings: switch the source to SMB, enter the server, then pick a guest share
and folder with the gamepad.

Each share needs one line in `/etc/fstab`, added once with sudo. That is what lets the app mount it without
a password. The share is mounted by the kernel and read-only, so nothing can write to it, and installers
aren't copied or cached. Run this on the Deck:

```bash
mkdir -p ~/.cache/gog-installer/smb
echo '//192.168.0.13/games /home/deck/.cache/gog-installer/smb cifs ro,guest,user,noauto,uid=deck,gid=deck,nofail 0 0' | sudo tee -a /etc/fstab
```

KDE's `kio-fuse` is not used: it can write to the share, and it returned no data for a read from the
middle of an installer.

## How it works

| Step | What happens |
|------|--------------|
| List | `sshfs` mounts the server folder read-only under `~/.cache/gog-installer/mnt` |
| Install | The installer runs from that mount under Proton, in `steamapps/compatdata/<id>` |
| Find the game | The primary play task in `goggame-*.info`, or you pick the executable |
| Add to Steam | Writes `shortcuts.vdf` and `CompatToolMapping` in `config.vdf`, backing up each as `*.gogi.bak` |

A game's `<id>` comes from its folder name, so it stays stable and Steam keeps finding its prefix and
saves. Renaming a game's folder on the server makes it look like a different game. State lives in
`~/.local/share/gog-installer/state.json`, and installer output goes to `install.log` beside it.

## Batch install and artwork

**X** selects games, **A** installs them one after another, and **Y** adds everything waiting to Steam in
one restart. Installers run with Inno Setup's `/SILENT` flags, so they show progress and ask nothing.

Artwork lands in Steam's `grid` folder when a game is added, or with `gog_installer.py --fetch-art`. Steam
reads it at startup. Sources, in order: an exact title match on Steam's public store data, then GOG's public
product data. If a game has no portrait cover, one is made from its wide art and title. Existing images
are never replaced.

## Development

```bash
python3 -m unittest test_gog_installer
python3 gog_installer.py --list          # on the Deck: what the server folder holds
```
