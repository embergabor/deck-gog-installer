# GOG Backup Installer for Steam Deck

Installs your GOG installer backups on the Steam Deck and adds the games to Steam.

## Install

```bash
./install.sh
```

Then add the app to Steam (this restarts Steam):

```bash
ssh deck@steamdeck.local 'systemd-run --user --collect python3 ~/.local/share/gog-installer/gog_installer.py --apply-steam'
```

## Sources

Keep one folder per game, with its `setup_*.exe` and `.bin` files inside.

- **SMB share:** Press **Start** in the app for Settings: switch the source to SMB, enter the server, then pick a guest share and folder with the gamepad.
- **USB drive or SD card:** Press **Start** in the app for Settings: switch the source to USB, then pick the drive and folder with the gamepad.

## Use

- **A** installs the selected game. **X** selects several to install one after another.
- **Y** adds installed games to Steam. Steam restarts once.
- **A** on an installed game opens its options: Proton version, fixes, and a log for the next launch. **Y** applies them.

## Credits

ProtonDB suggestions use the ProtonDB data export, [github.com/bdefore/protondb-data](https://github.com/bdefore/protondb-data) (ODbL).
