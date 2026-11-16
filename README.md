# GOG Backup Installer for Steam Deck

Installs your GOG installer backups on the Steam Deck and adds the games to Steam.

![The library: a grid of covers with a badge for each game's state](docs/screenshots/library.png)

## Install

On the Steam Deck, in Desktop Mode:

1. Download **GOG-Backup-Installer.desktop** from the [latest release](https://github.com/embergabor/deck-gog-installer/releases/latest).
2. Open your Downloads folder and double-click it. If Desktop Mode asks whether to run it, allow it.
3. Choose **Yes**. The app is installed, and Steam closes and reopens with **GOG Backup Installer** in your
   library under Non-Steam.

Switch back to Game Mode and start it from there.

The app updates itself. When a new version is out, an **Update** button shows at the top of the library and in
Settings; press it and the app restarts on the new version. Your sources, games and settings stay.

## Sources

Keep one folder per game, with its `setup_*.exe` and `.bin` files inside.

Press **View** (or **Start**) in the app for Settings, then **Add a source**:

- **Network share:** enter the server, then pick a guest SMB share and folder with the gamepad.
- **USB drive or SD card:** pick the drive and folder with the gamepad.

![Settings: sources and Proton defaults](docs/screenshots/settings.png)

## Use

The library is a grid of covers. A badge on each says where it stands: not installed, installed, or in Steam.
**L1** and **R1** switch between All, Not installed and Installed.

- **A** opens a game. On a game that isn't installed, **A** installs it.
- **X** picks several games; **Start** installs them one after another.
- **Y** adds installed games to Steam. Steam restarts once.
- On an installed game, **A** plays it through Steam. Below are its Proton version, fixes, and a log for the next launch; **Y** applies changes.
- Everything can also be tapped.

Covers come from Steam and GOG and are kept in `~/.cache/gog-installer/covers`. Until a cover is there, the game shows as a coloured tile with its title.

<p>
  <img src="docs/screenshots/details.png" alt="A game that isn't installed: sizes, ProtonDB rating, Install" width="49%">
  <img src="docs/screenshots/installed.png" alt="An installed game: Play and its Proton settings" width="49%">
</p>

![Installing, with the rest of the queue](docs/screenshots/installing.png)

## Credits

ProtonDB suggestions use the ProtonDB data export, [github.com/bdefore/protondb-data](https://github.com/bdefore/protondb-data) (ODbL).
