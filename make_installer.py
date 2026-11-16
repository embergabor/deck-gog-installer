#!/usr/bin/env python3
"""Builds dist/GOG-Backup-Installer.desktop: one file that installs the app on a Steam Deck in Desktop Mode.

Double-clicking it runs install.sh, which is stored in the file's "#> " lines; the app and its fonts are a
tar.gz in its "#= " lines. Both are comments to the desktop entry, so the file stays a valid launcher.

The app inside gets the version written into it, which is how it knows when a newer release is out, and a
.sha256 file is written beside the installer for the app to check its downloads against. Upload both.

Usage: python3 make_installer.py VERSION [output path]      e.g. 0.2.0; a build without one never updates
"""
import base64
import hashlib
import io
import os
import re
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "dist", "GOG-Backup-Installer.desktop")
APP_FILES = ["gog_installer.py", "gog-installer.sh", "cover.svg", "wide.svg"]
FONT_FILES = ["BarlowSemiCondensed-Regular.ttf", "BarlowSemiCondensed-Bold.ttf", "OFL.txt"]

# The launcher pipes its own "#> " lines to bash and passes its path on: %k is this file. In a desktop entry
# a quoted argument needs \" and \$, and each backslash is written twice, hence \\" and \\$.
EXEC = r'''bash -c "sed -n 's/^#> //p' \\"\\$0\\" | bash -s -- \\"\\$0\\"" %k'''

HEADER = f"""[Desktop Entry]
Type=Application
Name=Install GOG Backup Installer
Comment=Installs GOG Backup Installer on this Steam Deck and adds it to Steam
Icon=system-software-install
Terminal=false
Exec={EXEC}
"""


def stamped(data, version):
    """The app's source with VERSION set to this release's version."""
    old = b'VERSION = "dev"'
    if data.count(old) != 1:
        raise SystemExit("gog_installer.py needs exactly one line VERSION = \"dev\"")
    return data.replace(old, f'VERSION = "{version}"'.encode())


def payload(version="dev"):
    """The app and fonts as a tar.gz, the same bytes for the same files and version."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for folder, names, mode in (("app", APP_FILES, 0o644), ("fonts", FONT_FILES, 0o644)):
            base = HERE if folder == "app" else os.path.join(HERE, "fonts")
            for name in names:
                with open(os.path.join(base, name), "rb") as f:
                    data = f.read()
                if name == "gog_installer.py":
                    data = stamped(data, version)
                info = tarfile.TarInfo(f"{folder}/{name}")
                info.size, info.mtime = len(data), 0
                info.mode = 0o755 if name.endswith(".sh") else mode
                tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return raw[:4] + b"\0\0\0\0" + raw[8:]  # gzip's own timestamp, so rebuilding changes nothing


def build(out=OUT, version="dev"):
    with open(os.path.join(HERE, "install.sh")) as f:
        script = f.read()
    data = base64.b64encode(payload(version)).decode()
    lines = [HEADER, f"# Version {version}. The installer script, then the app itself. Built by make_installer.py.\n"]
    lines += [f"#> {line}\n" for line in script.splitlines()]
    lines += [f"#= {data[i:i + 76]}\n" for i in range(0, len(data), 76)]
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        f.write("".join(lines))
    os.chmod(out, 0o755)
    with open(out, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    with open(out + ".sha256", "w") as f:
        f.write(f"{digest}  {os.path.basename(out)}\n")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2 or not re.fullmatch(r"\d+(\.\d+)*", sys.argv[1]):
        raise SystemExit(__doc__)
    path = build(sys.argv[2] if len(sys.argv) > 2 else OUT, sys.argv[1])
    print(f"{path} ({os.path.getsize(path) // 1024} KB), version {sys.argv[1]}")
    print(f"{path}.sha256")
