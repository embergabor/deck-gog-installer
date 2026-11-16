#!/bin/bash
# Installs GOG Backup Installer for this user on a Steam Deck and adds it to Steam as a non-Steam game.
#
# Normally this runs from inside GOG-Backup-Installer.desktop (made by make_installer.py): double-clicking that
# file in Desktop Mode pipes these lines to bash with the .desktop file's path as $1, and the app comes out of
# its #= lines. From a copy of the repo it also runs as it is: ./install.sh
set -euo pipefail

TITLE="GOG Backup Installer"
APP="$HOME/.local/share/gog-installer"
FONTS="$HOME/.local/share/fonts/gog-installer"
MENU="$HOME/.local/share/applications"

say() {
    if command -v kdialog >/dev/null 2>&1; then kdialog --title "$TITLE" --msgbox "$1"
    elif command -v zenity >/dev/null 2>&1; then zenity --info --title="$TITLE" --text="$1"
    else echo "$1"; fi
}
fail() {
    if command -v kdialog >/dev/null 2>&1; then kdialog --title "$TITLE" --error "$1"
    elif command -v zenity >/dev/null 2>&1; then zenity --error --title="$TITLE" --text="$1"
    else echo "$1" >&2; fi
    exit 1
}
ask() {
    if command -v kdialog >/dev/null 2>&1; then kdialog --title "$TITLE" --yesno "$1"
    elif command -v zenity >/dev/null 2>&1; then zenity --question --title="$TITLE" --text="$1"
    else read -r -p "$1 [y/N] " answer && [[ "$answer" == [yY]* ]]; fi
}

python3 -c 'import tkinter' 2>/dev/null || fail "This installer is for the Steam Deck. Run it in Desktop Mode."

# Already in Steam: an update, which needs no restart. Otherwise adding it to Steam is part of installing.
in_steam=false
for f in "$HOME"/.local/share/Steam/userdata/*/config/shortcuts.vdf; do
    if [[ -f "$f" ]] && grep -q "gog-installer.sh" "$f"; then in_steam=true; fi
done
if ! $in_steam; then
    ask "Install $TITLE and add it to your Steam library?

Steam closes and reopens to add it, which takes about 20 seconds." || exit 0
fi

src="$(mktemp -d)"
trap 'rm -rf "$src"' EXIT
if [[ -n "${1:-}" ]]; then
    sed -n 's/^#= //p' "$1" | base64 -d | tar -xz -C "$src" 2>/dev/null \
        || fail "The installer file is damaged. Download it again."
else
    here="$(cd "$(dirname "$0")" && pwd)"
    mkdir -p "$src/app" "$src/fonts"
    cp "$here"/{gog_installer.py,gog-installer.sh,cover.svg,wide.svg} "$src/app/"
    cp "$here"/fonts/*.ttf "$here/fonts/OFL.txt" "$src/fonts/"
fi

mkdir -p "$APP" "$FONTS" "$MENU"
cp -f "$src"/app/* "$APP/"   # settings, state and logs live beside the app and are left as they are
chmod +x "$APP/gog-installer.sh"
cp -f "$src"/fonts/* "$FONTS/"
fc-cache -f "$FONTS" >/dev/null 2>&1 || true

# so it can also be opened from the application menu in Desktop Mode
cat > "$MENU/gog-backup-installer.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$TITLE
Comment=Install your GOG installer backups and add them to Steam
Exec="$APP/gog-installer.sh"
Icon=$APP/cover.svg
Categories=Game;
Terminal=false
EOF

if $in_steam; then
    say "$TITLE is updated. It's in your Steam library under Non-Steam."
    exit 0
fi

say "$TITLE is installed. Press OK and Steam closes and reopens with it in your library, under Non-Steam.

Then switch to Game Mode to use it with the controller."
# Adds the app as a non-Steam game, with its artwork. In its own unit: closing Steam must not end it.
if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --user --collect --unit="gog-installer-setup-$(date +%s)" \
        python3 "$APP/gog_installer.py" --apply-steam >/dev/null
else
    nohup python3 "$APP/gog_installer.py" --apply-steam >/dev/null 2>&1 &
fi
