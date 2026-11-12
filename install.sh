#!/bin/bash
# Copy GOG Backup Installer to a Steam Deck over SSH. Doesn't touch Steam; the app adds itself on request.
# Usage: ./install.sh [user@host]      (default deck@steamdeck.local)
# Env:   SSH_KEY=path/to/key           (default ~/.ssh/id_deck_wavesafe if it exists)
set -euo pipefail

HOST="${1:-deck@steamdeck.local}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
KEY="${SSH_KEY:-$HOME/.ssh/id_deck_wavesafe}"
[[ -f "$KEY" ]] && SSH_OPTS+=(-i "$KEY")

ssh "${SSH_OPTS[@]}" "$HOST" 'mkdir -p ~/.local/share/gog-installer ~/.local/share/fonts/gog-installer'
scp -q "${SSH_OPTS[@]}" "$HERE/gog_installer.py" "$HERE/gog-installer.sh" "$HERE/cover.svg" "$HERE/wide.svg" "$HOST:.local/share/gog-installer/"
scp -q "${SSH_OPTS[@]}" "$HERE"/fonts/*.ttf "$HERE/fonts/OFL.txt" "$HOST:.local/share/fonts/gog-installer/"
ssh "${SSH_OPTS[@]}" "$HOST" 'chmod +x ~/.local/share/gog-installer/gog-installer.sh; fc-cache -f ~/.local/share/fonts/gog-installer'
echo "Copied to $HOST:~/.local/share/gog-installer"
