#!/bin/bash
# What the Steam shortcut starts.
exec python3 "$(dirname "$(readlink -f "$0")")/gog_installer.py" "$@"
