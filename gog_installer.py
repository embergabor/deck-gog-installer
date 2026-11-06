#!/usr/bin/env python3
"""GOG Installer: a fullscreen, gamepad-first Steam Deck app.

Lists GOG installers on a server folder (mounted read-only with sshfs), runs the chosen one
under Proton without copying it to the Deck, and adds the finished games to Steam.
Needs only what SteamOS ships: python3 with tkinter and evdev, sshfs, and Steam's Proton.

Usage: gog_installer.py                 run the app
       gog_installer.py --list          print what the server folder holds, then exit
       gog_installer.py --apply-steam   add pending games to Steam (restarts Steam)
       gog_installer.py --fetch-art     download missing artwork; Steam shows it after its next restart
"""
import base64
import binascii
import glob
import html
import json
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import zlib
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA = f"{HOME}/.local/share/gog-installer"
STATE_FILE = f"{DATA}/state.json"
CONFIG_FILE = f"{HOME}/.config/gog-installer/config.json"
LOG_FILE = f"{DATA}/install.log"
APPLY_LOG = f"{DATA}/apply-steam.log"
MOUNT = f"{HOME}/.cache/gog-installer/mnt"
STEAM = f"{HOME}/.local/share/Steam"
COMMON = f"{STEAM}/steamapps/common"
COMPAT_TOOL = ("Proton - Experimental", "proton_experimental")
SELF_NAME = "GOG Installer"
SILENT_FLAGS = ["/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART"]  # Inno Setup: progress window, no questions
SIZES_FILE = f"{DATA}/gog-lookup.json"

DEFAULT_CONFIG = {
    "source": "ssh",  # or "smb"
    "host": "embergabor@192.168.0.13",
    "path": "/mnt/4tba/games/gog",
    "key": f"{HOME}/.ssh/id_drm_server",
    "smb_server": "192.168.0.13",
    "smb_share": "games",
    "smb_folder": "gog",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"games": {}}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=1)


def save_state(state):
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# --- server folder ---------------------------------------------------------------------

def mount_server(cfg, mount=MOUNT):
    """Mount the installer folder read-only. Returns an error message, or None."""
    os.makedirs(mount, exist_ok=True)
    if os.path.ismount(mount):
        try:
            os.listdir(mount)
            return None
        except OSError:  # stale mount from a dropped connection
            unmount_server(mount)
    opts = ",".join(["ro", "reconnect", "ServerAliveInterval=15", "ConnectTimeout=6", "BatchMode=yes",
                     "IdentitiesOnly=yes", "StrictHostKeyChecking=accept-new", f"IdentityFile={cfg['key']}"])
    try:
        r = subprocess.run(["sshfs", "-o", opts, f"{cfg['host']}:{cfg['path']}", mount],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return None if r.returncode == 0 else (r.stderr.strip() or "sshfs failed")


def unmount_server(mount=MOUNT):
    subprocess.run(["fusermount3", "-uz", mount], capture_output=True)


# --- SMB guest shares --------------------------------------------------------------------
# Mounted read-only by the kernel, which needs one line in /etc/fstab per share (see README).

def parse_shares(output):
    """Disk shares from `smbclient -g -L`, leaving out the hidden ones such as print$."""
    out = []
    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) >= 2 and parts[0] == "Disk" and not parts[1].endswith("$"):
            out.append(parts[1])
    return sorted(out, key=str.lower)


DIR_LINE = re.compile(r"^  (.+?)\s+([A-Z]*)\s+(\d+)\s+\w{3} \w{3}\s+\d+ [\d:]+ \d{4}$")


def parse_folders(output):
    """Sub-folders from a smbclient `ls`."""
    out = []
    for line in output.splitlines():
        m = DIR_LINE.match(line)
        if m and "D" in m.group(2) and m.group(1) not in (".", "..") and not m.group(1).startswith("."):
            out.append(m.group(1))
    return sorted(out, key=str.lower)


def smb_shares(server):
    r = subprocess.run(["smbclient", "-N", "-g", "-L", f"//{server}"], capture_output=True, text=True, timeout=15)
    return parse_shares(r.stdout)


def smb_folders(server, share, folder):
    if '"' in folder:
        return []
    cmd = (f'cd "{folder}"; ls' if folder else "ls")
    r = subprocess.run(["smbclient", "-N", f"//{server}/{share}", "-c", cmd], capture_output=True, text=True, timeout=15)
    return parse_folders(r.stdout)


def fstab_mountpoint(server, share, text):
    """Where /etc/fstab lets this user mount the share, or None."""
    want = f"//{server}/{share}".lower()
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and not f[0].startswith("#") and f[0].lower() == want and f[2] in ("cifs", "smb3") \
                and "user" in f[3].split(",") and "ro" in f[3].split(","):
            return f[1].replace("\\040", " ")
    return None


def mount_smb(cfg):
    """(folder path, error)"""
    server, share, folder = cfg["smb_server"], cfg["smb_share"], cfg["smb_folder"].strip("/")
    try:
        with open("/etc/fstab") as f:
            mountpoint = fstab_mountpoint(server, share, f.read())
    except OSError:
        mountpoint = None
    try:
        if not mountpoint:
            return None, f"//{server}/{share} isn't set up on this Deck yet. It needs a read-only line in /etc/fstab."
        if not os.path.ismount(mountpoint):
            os.makedirs(mountpoint, exist_ok=True)
            r = subprocess.run(["mount", mountpoint], capture_output=True, text=True, timeout=20)
            if r.returncode:
                return None, r.stderr.strip() or "mount failed"
        return os.path.join(mountpoint, folder), None
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)


def connect_source(cfg):
    """Make the installer folder available. Returns (folder path, error)."""
    if cfg.get("source") == "smb":
        return mount_smb(cfg)
    err = mount_server(cfg)
    return (None, err) if err else (MOUNT, None)


def disconnect_source(cfg):
    if cfg.get("source") != "smb":
        unmount_server()
        return
    try:
        with open("/etc/fstab") as f:
            mountpoint = fstab_mountpoint(cfg["smb_server"], cfg["smb_share"], f.read())
        if mountpoint and os.path.ismount(mountpoint):
            subprocess.run(["umount", mountpoint], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def source_label(cfg):
    if cfg.get("source") == "smb":
        return f"{cfg['smb_server']}", f"{cfg['smb_share']}/{cfg['smb_folder']}".rstrip("/")
    return cfg["host"].split("@")[-1], cfg["path"]


def parse_setup(installer):
    """setup_hollow_knight_1.5.12620_(64bit)_(89718).exe -> ("hollow_knight", "1.5.12620")"""
    stem = re.sub(r"\.exe$", "", os.path.basename(installer), flags=re.I)
    stem = re.sub(r"^setup_", "", stem, flags=re.I)
    # cut at the version, not at any number: keep the 2 in baldurs_gate_2_v2.6
    parts = re.split(r"_(?=v?\d+\.\d|v\d|\d{4,}|\()", stem, maxsplit=1)
    version = parts[1] if len(parts) > 1 else ""
    # GOG's download number and the bitness aren't part of the build name
    version = re.sub(r"_\((?:\d+|\d+bit)\)", "", version)
    return parts[0], version


def clean_title(installer):
    """setup_hollow_knight_1.5.12620_(64bit)_(89718).exe -> Hollow Knight"""
    words = parse_setup(installer)[0].replace("_", " ").split()
    small = {"a", "an", "and", "at", "for", "in", "of", "on", "or", "the", "to"}
    return " ".join(w.capitalize() if i == 0 or w not in small else w for i, w in enumerate(words))


def scan_folder(root):
    """One entry per game folder that holds a setup_*.exe directly inside it (Extras is ignored)."""
    games = []
    for name in sorted(os.listdir(root), key=str.lower):
        folder = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(folder):
            continue
        files = [f for f in os.listdir(folder) if not f.startswith(".")]
        setups = [f for f in files if re.match(r"setup_.*\.exe$", f, re.I)]
        if not setups:
            continue
        # the base game is the shortest name; DLC installers add to it
        setup = sorted(setups, key=lambda f: (len(f), f))[0]
        stem = os.path.splitext(setup)[0]
        parts = [setup] + [f for f in files if f.startswith(stem + "-") and f.lower().endswith(".bin")]
        size = sum(os.path.getsize(os.path.join(folder, f)) for f in parts)
        games.append({"slug": name, "title": clean_title(setup), "setup": setup, "size": size})
    return games


def shortcut_appid(slug):
    """Stable for a game folder, so its Proton prefix and saves are always found again."""
    crc = binascii.crc32(f"gog-installer:{slug}".encode()) | 0x80000000
    return crc, crc - 2 ** 32  # unsigned (compatdata, config.vdf), signed (shortcuts.vdf)


def prefix_dir(slug):
    return f"{STEAM}/steamapps/compatdata/{shortcut_appid(slug)[0]}"


def fmt_size(n):
    if n >= 1000 ** 3:
        return f"{n / 1000 ** 3:.2f} GB" if n < 10 * 1000 ** 3 else f"{n / 1000 ** 3:.1f} GB"
    return f"{n / 1000 ** 2:.0f} MB"


def fmt_free(n):
    return f"{n / 1000 ** 3:.0f} GB"


def free_space():
    st = os.statvfs(HOME)
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


def install_size(game, state):
    """The exact installed size if known (measured, or from GOG for this build), else None."""
    return state["games"].get(game["slug"], {}).get("installed_size") or game.get("install_size")


def needed_bytes(game, state):
    """For the space check: the exact size, or the installer's size, which it can't be smaller than."""
    return install_size(game, state) or game["size"]


# --- GOG's public data -------------------------------------------------------------------
# No login needed. A size is only used when both the game and the installer's build match exactly.

def decode_json(data):
    try:
        data = zlib.decompress(data, 47)  # GOG's build files are compressed
    except zlib.error:
        pass
    return json.loads(data)


def clean_store_title(title):
    return re.sub(r"\s+", " ", re.sub(r"[®™]", "", title)).strip()


def match_catalog(name, products):
    want = normalize_title(name)
    for p in products:
        if want in (normalize_title(p.get("slug", "")), normalize_title(p.get("title", ""))):
            return p
    return None


def match_build(version, items):
    want = normalize_title(version)
    if not want:
        return None
    return next((i for i in items if normalize_title(i.get("version_name") or "") == want), None)


def depots_size(meta, product_id):
    """Installed bytes for the English install: language-neutral and English depots of this product."""
    total = 0
    for d in meta.get("depots", []):
        langs = d.get("languages", [])
        if str(d.get("productId", product_id)) == str(product_id) and any(l in ("*", "en", "en-US") for l in langs):
            total += d.get("size", 0)
    return total or None


def gog_lookup(setup):
    """{"id", "title", "install_size"} for an installer; {} if the game isn't an exact catalog match."""
    name, version = parse_setup(setup)
    query = urllib.parse.quote(name.replace("_", " "))
    found = decode_json(http_get(f"https://catalog.gog.com/v1/catalog?limit=8&query=like:{query}"
                                 "&order=desc:score&productType=in:game,pack"))
    product = match_catalog(name, found.get("products", []))
    if not product:
        return {}
    pid = str(product["id"])
    info = {"id": pid, "title": clean_store_title(product.get("title", "")), "install_size": None}
    builds = decode_json(http_get(f"https://content-system.gog.com/products/{pid}/os/windows/builds?generation=2"))
    build = match_build(version, builds.get("items", []))
    if build:
        info["install_size"] = depots_size(decode_json(http_get(build["link"])), pid)
    return info


def load_lookups():
    try:
        with open(SIZES_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_lookups(cache):
    os.makedirs(DATA, exist_ok=True)
    with open(SIZES_FILE, "w") as f:
        json.dump(cache, f, indent=1)


def apply_lookup(game, info):
    if info.get("title"):
        game["title"] = info["title"]
    game["install_size"] = info.get("install_size")
    game["gog_id"] = info.get("id", "")


# --- running an installer --------------------------------------------------------------

def find_runtime(proton_dir):
    """The Steam runtime this Proton asks for, from its toolmanifest and Steam's app manifests."""
    with open(f"{proton_dir}/toolmanifest.vdf", errors="replace") as f:
        m = re.search(r'"require_tool_appid"\s+"(\d+)"', f.read())
    if m:
        with open(f"{STEAM}/steamapps/appmanifest_{m.group(1)}.acf", errors="replace") as f:
            d = re.search(r'"installdir"\s+"([^"]+)"', f.read())
        if d:
            return f"{COMMON}/{d.group(1)}"
    return f"{COMMON}/SteamLinuxRuntime_sniper"


def installer_command(setup_path, slug):
    proton = f"{COMMON}/{COMPAT_TOOL[0]}"
    runtime = find_runtime(proton)
    env = dict(os.environ, STEAM_COMPAT_DATA_PATH=prefix_dir(slug), STEAM_COMPAT_CLIENT_INSTALL_PATH=STEAM,
               SteamAppId=str(shortcut_appid(slug)[0]), SteamGameId=str(shortcut_appid(slug)[0]))
    cmd = [f"{runtime}/_v2-entry-point", "--verb=waitforexitandrun", "--",
           f"{proton}/proton", "waitforexitandrun", setup_path] + SILENT_FLAGS
    return cmd, env


def find_game(slug):
    """After an install: (play task dict or None, list of candidate exe paths)."""
    drive_c = f"{prefix_dir(slug)}/pfx/drive_c"
    skip = {"windows", "users", "programdata", "program files", "program files (x86)"}
    infos, exes = [], []
    for root, dirs, files in os.walk(drive_c):
        if root == drive_c:
            dirs[:] = [d for d in dirs if d.lower() not in skip]
        for f in files:
            low = f.lower()
            if re.match(r"goggame-\d+\.info$", low):
                infos.append(os.path.join(root, f))
            elif low.endswith(".exe") and not low.startswith(("unins", "dxsetup", "vc_redist", "vcredist")):
                exes.append(os.path.join(root, f))
    for path in sorted(infos):
        task = parse_goggame(path)
        if task and os.path.exists(task["exe"]):
            return task, exes
    return None, sorted(exes)


def parse_goggame(path):
    """The primary play task of a goggame-*.info, with absolute paths."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    tasks = info.get("playTasks") or []
    task = next((t for t in tasks if t.get("isPrimary") and t.get("path")), None) \
        or next((t for t in tasks if t.get("type") == "FileTask" and t.get("path")), None)
    if not task:
        return None
    root = os.path.dirname(path)
    exe = os.path.join(root, task["path"].replace("\\", "/"))
    work = task.get("workingDir") or ""
    return {"exe": exe, "workdir": os.path.join(root, work.replace("\\", "/")) if work else os.path.dirname(exe),
            "args": task.get("arguments") or "", "root": root, "title": info.get("name") or "",
            "gog_id": str(info.get("gameId") or info.get("rootGameId") or "")}


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class InstallJob(threading.Thread):
    """Runs one installer under Proton. Posts ("done", task, exes) or ("error", message)."""

    def __init__(self, game, out, root):
        super().__init__(daemon=True)
        self.game, self.out, self.root, self.proc, self.stopped = game, out, root, None, False

    def run(self):
        try:
            setup = os.path.join(self.root, self.game["slug"], self.game["setup"])
            cmd, env = installer_command(setup, self.game["slug"])
            os.makedirs(prefix_dir(self.game["slug"]), exist_ok=True)
            os.makedirs(DATA, exist_ok=True)
            with open(LOG_FILE, "w") as log:
                self.proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=True, cwd=os.path.dirname(setup))
                self.proc.wait()
            if self.stopped:
                self.out.put(("stopped",))
                return
            task, exes = find_game(self.game["slug"])
            self.out.put(("done", task, exes))
        except (OSError, subprocess.SubprocessError) as e:
            self.out.put(("error", str(e)))

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass


def record_install(state, game, task):
    """Remember what Steam should launch; called once the executable is known."""
    root = task.get("root") or os.path.dirname(task["exe"])
    icons = glob.glob(f"{glob.escape(root)}/goggame-*.ico")
    state["games"][game["slug"]] = {
        "title": task.get("title") or game["title"], "exe": task["exe"], "workdir": task["workdir"],
        "args": task.get("args", ""), "installed_size": dir_size(root), "in_steam": False,
        "gog_id": task.get("gog_id") or game.get("gog_id", ""), "icon": icons[0] if icons else "",
    }
    save_state(state)


def discard_prefix(slug, state):
    """Remove a prefix only for a game that never finished installing, so saves can't be lost."""
    if slug not in state["games"] and os.path.isdir(prefix_dir(slug)):
        shutil.rmtree(prefix_dir(slug), ignore_errors=True)


# --- Steam: shortcuts.vdf (binary) -------------------------------------------------------

def vdf_load(data):
    def read_str(i):
        end = data.index(b"\x00", i)
        return data[i:end].decode("utf-8", "replace"), end + 1

    def read_map(i):
        out = {}
        while True:
            t = data[i]
            i += 1
            if t == 0x08:
                return out, i
            key, i = read_str(i)
            if t == 0x00:
                out[key], i = read_map(i)
            elif t == 0x01:
                out[key], i = read_str(i)
            elif t == 0x02:
                out[key] = struct.unpack_from("<i", data, i)[0]
                i += 4
            else:
                raise ValueError(f"unsupported VDF type {t:#x} at {i}")

    return read_map(0)[0] if data else {"shortcuts": {}}


def vdf_dump(d):
    out = b""
    for k, v in d.items():
        key = k.encode() + b"\x00"
        if isinstance(v, dict):
            out += b"\x00" + key + vdf_dump(v) + b"\x08"
        elif isinstance(v, int):
            out += b"\x02" + key + struct.pack("<i", v)
        else:
            out += b"\x01" + key + str(v).encode() + b"\x00"
    return out


def shortcut_entry(signed, name, exe, workdir, args, icon=""):
    return {
        "appid": signed, "appname": name, "Exe": f'"{exe}"', "StartDir": f'"{workdir}"', "icon": icon,
        "ShortcutPath": "", "LaunchOptions": args, "IsHidden": 0, "AllowDesktopConfig": 1, "AllowOverlay": 1,
        "OpenVR": 0, "Devkit": 0, "DevkitGameID": "", "DevkitOverrideAppID": 0, "LastPlayTime": 0,
        "FlatpakAppID": "", "sortas": "", "tags": {"0": "GOG"},
    }


def merge_shortcuts(data, entries):
    """shortcuts.vdf bytes with entries added or replaced by appid; all others stay as they are."""
    vdf = vdf_load(data)
    existing = list(vdf["shortcuts"].values())
    by_id = {e["appid"]: e for e in entries}
    kept = []
    for e in existing:
        new = by_id.pop(e.get("appid"), None)
        if new:
            new["LastPlayTime"] = e.get("LastPlayTime", 0)
            kept.append(new)
        else:
            kept.append(e)
    kept += list(by_id.values())
    vdf["shortcuts"] = {str(i): e for i, e in enumerate(kept)}
    return vdf_dump(vdf) + b"\x08"


def set_compat_tool(text, appid, tool):
    """config.vdf text with appid mapped to tool inside CompatToolMapping; nothing else changes."""
    m = re.search(r'^([ \t]*)"CompatToolMapping"[ \t]*\r?\n[ \t]*\{[ \t]*\r?\n', text, re.M)
    if not m:
        raise ValueError("CompatToolMapping not found in config.vdf")
    start = m.end()
    depth, end = 1, None
    for t in re.finditer(r'"(?:[^"\\]|\\.)*"|[{}]', text[start:]):
        if t.group() == "{":
            depth += 1
        elif t.group() == "}":
            depth -= 1
            if depth == 0:
                end = start + t.start()
                break
    if end is None:
        raise ValueError("unbalanced CompatToolMapping in config.vdf")
    ind = m.group(1) + "\t"
    block = (f'{ind}"{appid}"\n{ind}{{\n{ind}\t"name"\t\t"{tool}"\n{ind}\t"config"\t\t""\n'
             f'{ind}\t"priority"\t\t"250"\n{ind}}}\n')
    body = text[start:end]
    # drop an older mapping for this app, then add ours at the end of the block
    body = re.sub(r'^[ \t]*"%d"[ \t]*\r?\n[ \t]*\{.*?^[ \t]*\}[ \t]*\r?\n' % appid, "", body, flags=re.M | re.S)
    # the closing brace's own indentation sits at the end of body: keep it after the new block
    tail = re.search(r"[ \t]*$", body).group()
    body = body[:len(body) - len(tail)] + block + tail
    return text[:start] + body + text[end:]


# --- artwork -----------------------------------------------------------------------------
# Steam reads art for a shortcut from userdata/<user>/config/grid at startup. Never replaces a file
# that already exists, so art set by hand stays.

STEAM_CDN = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps"
STEAM_ART = {"p": "library_600x900_2x.jpg", "": "header.jpg", "_hero": "library_hero.jpg", "_logo": "logo.png"}


def image_ext(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None


def http_get(url, timeout=12, limit=20 << 20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 GOGInstaller"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def normalize_title(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_steam_app(title, items):
    """Exact title match only: a wrong cover is worse than none."""
    want = normalize_title(title)
    for item in items:
        if item.get("type") == "app" and normalize_title(item.get("name", "")) == want:
            return item["id"]
    return None


def steam_art_urls(title):
    query = urllib.parse.quote(title)
    data = http_get(f"https://store.steampowered.com/api/storesearch/?term={query}&l=english&cc=US")
    app = match_steam_app(title, json.loads(data).get("items", []))
    return {k: f"{STEAM_CDN}/{app}/{name}" for k, name in STEAM_ART.items()} if app else {}


def gog_art_urls(gog_id):
    """GOG's public product data has no portrait cover, only wide images."""
    images = json.loads(http_get(f"https://api.gog.com/products/{gog_id}")).get("images", {})
    fix = lambda u: ("https:" + u) if u and u.startswith("//") else u
    out = {}
    if images.get("logo2x"):
        out[""] = fix(images["logo2x"])
    if images.get("background"):
        out["_hero"] = fix(images["background"])
    return out


def has_art(grid, appid, suffix):
    return bool(glob.glob(f"{glob.escape(grid)}/{appid}{suffix}.*"))


def cover_svg(title, image, ext):
    """A portrait cover for games with no store cover: their wide art, cropped, with the title on a band."""
    lines, line = [], ""
    for word in title.split():
        if line and len(line) + len(word) > 15:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines = (lines + [line])[:4]
    top = 900 - 76 * len(lines) - 56
    text = "".join(f'<text x="40" y="{top + 76 * (i + 1)}" font-family="Barlow Semi Condensed" font-weight="700" '
                   f'font-size="70" fill="#F4EFFF">{html.escape(l)}</text>' for i, l in enumerate(lines))
    data = base64.b64encode(image).decode()
    mime = "image/png" if ext == "png" else "image/jpeg"
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="600" height="900">'
            f'<image width="600" height="900" preserveAspectRatio="xMidYMid slice" xlink:href="data:{mime};base64,{data}"/>'
            f'<rect y="{top}" width="600" height="{900 - top}" fill="#1C0F36" fill-opacity="0.92"/>{text}</svg>')


def make_cover(title, appid, grid, log=print):
    """Only when no cover exists and there is wide art to build one from."""
    if has_art(grid, appid, "p"):
        return
    sources = glob.glob(f"{glob.escape(grid)}/{appid}_hero.*") or glob.glob(f"{glob.escape(grid)}/{appid}.*")
    if not sources:
        return
    with open(sources[0], "rb") as f:
        image = f.read()
    ext = image_ext(image)
    if not ext:
        return
    r = subprocess.run(["rsvg-convert", "-o", f"{grid}/{appid}p.png"], capture_output=True,
                       input=cover_svg(title, image, ext).encode())
    log(f"made a cover for {title}: {'ok' if r.returncode == 0 else 'failed'}")


def fetch_art_for(title, appid, gog_id, grids, log=print, also=""):
    missing = [k for k in STEAM_ART if not all(has_art(g, appid, k) for g in grids)]
    if not missing:
        return
    urls = {}
    for source, arg in ((steam_art_urls, title), (steam_art_urls, also), (gog_art_urls, gog_id)):
        if not arg:
            continue
        try:
            for k, u in source(arg).items():
                urls.setdefault(k, u)
        except (OSError, ValueError) as e:
            log(f"art lookup failed for {title}: {e}")
    saved = []
    for k in missing:
        if k not in urls:
            continue
        try:
            data = http_get(urls[k])
        except (OSError, ValueError):
            continue
        ext = image_ext(data)
        if not ext:
            continue
        for g in grids:
            os.makedirs(g, exist_ok=True)
            if not has_art(g, appid, k):
                with open(f"{g}/{appid}{k}.{ext}", "wb") as f:
                    f.write(data)
        saved.append(k or "wide")
    log(f"art for {title}: {', '.join(saved) or 'none found'}")


def self_art(grids, log=print):
    """GOG Installer's own cover, drawn from the SVGs that ship with the app."""
    appid = shortcut_appid("__self__")[0]
    for suffix, svg in (("p", "cover.svg"), ("", "wide.svg")):
        source = f"{APP_DIR}/{svg}"
        for g in grids:
            if os.path.exists(source) and not has_art(g, appid, suffix):
                os.makedirs(g, exist_ok=True)
                r = subprocess.run(["rsvg-convert", source, "-o", f"{g}/{appid}{suffix}.png"], capture_output=True)
                log(f"own art {svg}: {'ok' if r.returncode == 0 else 'failed'}")


def refresh_from_gog_info(state):
    """Games installed by an older version: take the proper name, id and icon from GOG's info file."""
    changed = False
    for g in state["games"].values():
        if g.get("gog_id"):
            continue
        for info in glob.glob(f"{glob.escape(os.path.dirname(g['exe']))}/goggame-*.info"):
            task = parse_goggame(info)
            if task:
                g["gog_id"] = task["gog_id"]
                if not g.get("in_steam") and task["title"]:
                    g["title"] = task["title"]
                icons = glob.glob(f"{glob.escape(task['root'])}/goggame-*.ico")
                g["icon"] = icons[0] if icons else ""
                changed = True
                break
    if changed:
        save_state(state)


def fetch_all_art(state, log=print):
    refresh_from_gog_info(state)
    grids = [f"{STEAM}/userdata/{u}/config/grid" for u in steam_users()]
    try:
        self_art(grids, log)
    except OSError as e:
        log(f"own art failed: {e}")
    for slug, g in state["games"].items():
        appid = shortcut_appid(slug)[0]
        # the install folder usually carries the full name when the title came from a filename
        fetch_art_for(g["title"], appid, g.get("gog_id", ""), grids, log, os.path.basename(os.path.dirname(g["exe"])))
        for grid in grids:
            try:
                make_cover(g["title"], appid, grid, log)
            except OSError as e:
                log(f"cover failed for {g['title']}: {e}")


def steam_users():
    base = f"{STEAM}/userdata"
    return [d for d in os.listdir(base) if d.isdigit() and d != "0"] if os.path.isdir(base) else []


def pending_games(state):
    return {s: g for s, g in state["games"].items() if not g.get("in_steam")}


def write_steam_files(state, include_self=True):
    """Write shortcuts and Proton settings. Steam must be stopped. Returns the number of games added."""
    pending = pending_games(state)
    entries = []
    for slug, g in pending.items():
        entries.append(shortcut_entry(shortcut_appid(slug)[1], g["title"], g["exe"], g["workdir"], g["args"], g.get("icon", "")))
    if include_self:
        launcher = f"{APP_DIR}/gog-installer.sh"
        entries.append(shortcut_entry(shortcut_appid("__self__")[1], SELF_NAME, launcher, APP_DIR, ""))
    for user in steam_users():
        path = f"{STEAM}/userdata/{user}/config/shortcuts.vdf"
        data = b""
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            shutil.copy(path, path + ".gogi.bak")
        out = merge_shortcuts(data, [dict(e) for e in entries])
        with open(path + ".tmp", "wb") as f:
            f.write(out)
        os.replace(path + ".tmp", path)
    if pending:
        cfg = f"{STEAM}/config/config.vdf"
        with open(cfg, encoding="utf-8", errors="surrogateescape", newline="") as f:
            text = f.read()
        shutil.copy(cfg, cfg + ".gogi.bak")
        for slug in pending:
            text = set_compat_tool(text, shortcut_appid(slug)[0], COMPAT_TOOL[1])
        with open(cfg + ".tmp", "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
        os.replace(cfg + ".tmp", cfg)
    for g in pending.values():
        g["in_steam"] = True
    save_state(state)
    return len(pending)


def apply_steam():
    """Worker, outside Steam's cgroup. In Game Mode systemd restarts Steam right after it stops
    (gamescope-session.target Upholds steam-launcher), so the files are written straight away."""
    state = load_state()
    os.makedirs(DATA, exist_ok=True)
    with open(APPLY_LOG, "a") as log:
        log.write(f"--- {time.ctime()}\n")
        log.flush()
        # slow downloads first, while Steam is still up; art is read when Steam starts
        fetch_all_art(state, lambda m: (log.write(m + "\n"), log.flush()))
        game_mode = subprocess.run(["systemctl", "--user", "-q", "is-active", "steam-launcher.service"]).returncode == 0
        try:
            if game_mode:
                subprocess.run(["systemctl", "--user", "stop", "steam-launcher.service"], check=False)
            else:
                subprocess.run(["steam", "-shutdown"], check=False)
                for _ in range(60):
                    if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode != 0:
                        break
                    time.sleep(1)
            n = write_steam_files(state)
            log.write(f"added {n} game(s)\n")
        except Exception as e:  # Steam must come back whatever happened
            log.write(f"failed: {e}\n")
        finally:
            if game_mode:
                subprocess.run(["systemctl", "--user", "start", "steam-launcher.service"], check=False)
            else:
                subprocess.Popen(["steam"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_apply_steam():
    """Hand over to apply_steam in its own unit: stopping Steam ends this app."""
    subprocess.Popen(["systemd-run", "--user", "--collect", f"--unit=gog-installer-apply-{int(time.time())}",
                      sys.executable, os.path.abspath(__file__), "--apply-steam"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- gamepad -----------------------------------------------------------------------------

class Gamepad:
    """Reads every gamepad through evdev and turns it into actions: up, down, a, b, x, y, menu.
    Steam's virtual pad and the real one can both report a press, so held state is shared."""

    REPEAT_DELAY, REPEAT_EVERY = 0.35, 0.09

    def __init__(self):
        self.devices, self.held, self.next_repeat, self.axis = [], {}, {}, {}
        try:
            import evdev
            self.ecodes = evdev.ecodes
            for path in evdev.list_devices():
                try:
                    dev = evdev.InputDevice(path)
                    keys = dev.capabilities().get(self.ecodes.EV_KEY, [])
                    if self.ecodes.BTN_SOUTH in keys:
                        os.set_blocking(dev.fd, False)
                        self.devices.append(dev)
                    else:
                        dev.close()
                except OSError:
                    pass
        except ImportError:
            pass

    def _set(self, action, source, down, out):
        holders = self.held.setdefault(action, set())
        was = bool(holders)
        (holders.add if down else holders.discard)(source)
        if down and not was:
            out.append(action)
            self.next_repeat[action] = time.monotonic() + self.REPEAT_DELAY

    def poll(self):
        out = []
        e = getattr(self, "ecodes", None)
        for dev in self.devices:
            try:
                events = list(dev.read())
            except BlockingIOError:
                continue
            except OSError:
                continue
            for ev in events:
                src = dev.path
                if ev.type == e.EV_KEY:
                    action = {e.BTN_SOUTH: "a", e.BTN_EAST: "b", 307: "x", 308: "y",  # 307/308: BTN_X/BTN_Y
                              e.BTN_START: "menu",
                              e.BTN_DPAD_UP: "up", e.BTN_DPAD_DOWN: "down"}.get(ev.code)
                    if action and ev.value in (0, 1):
                        self._set(action, src, ev.value == 1, out)
                elif ev.type == e.EV_ABS and ev.code in (e.ABS_HAT0Y, e.ABS_Y):
                    if ev.code == e.ABS_HAT0Y:
                        val = ev.value
                    else:
                        info = dev.absinfo(e.ABS_Y)
                        span = max(1, info.max - info.min)
                        norm = (ev.value - info.min) / span * 2 - 1
                        val = -1 if norm < -0.6 else 1 if norm > 0.6 else 0
                    source = (src, ev.code)
                    self._set("up", source, val < 0, out)
                    self._set("down", source, val > 0, out)
        now = time.monotonic()
        for action in ("up", "down"):
            if self.held.get(action) and now >= self.next_repeat.get(action, 0):
                out.append(action)
                self.next_repeat[action] = now + self.REPEAT_EVERY
        return out


# --- UI ----------------------------------------------------------------------------------

GROUND, BAND, LINE, ROW_LINE = "#1C0F36", "#2A1650", "#4A2F7F", "#33205C"
TEXT, MUTED, DIM, FOCUS, CHIP, WARN, WARN_ON_FOCUS = (
    "#F4EFFF", "#C9BBEC", "#A08DCB", "#E4D8FF", "#3A1D78", "#FFC56B", "#6B3D00")
VISIBLE_ROWS = 7


class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import font as tkfont
        self.tk, self.tkfont = tk, tkfont
        self.root = tk.Tk()
        self.root.title(SELF_NAME)
        self.root.configure(bg=GROUND)
        if os.environ.get("GOGI_WINDOWED"):
            self.w, self.h = 1280, 800
            self.root.geometry("1280x800+0+0")
        else:
            self.root.attributes("-fullscreen", True)
            self.w, self.h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.scale = min(self.w / 1280, self.h / 800)
        self.canvas = tk.Canvas(self.root, width=self.w, height=self.h, bg=GROUND, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        families = set(tkfont.families())
        self.family = next((f for f in ("Barlow Semi Condensed", "Noto Sans", "DejaVu Sans") if f in families),
                           "TkDefaultFont")
        self.fonts, self.hits = {}, []
        self.cfg, self.state = load_config(), load_state()
        self.games, self.selected, self.top = [], 0, 0
        self.screen, self.job, self.current = "connecting", None, None
        self.events = queue.Queue()
        self.stop_armed_until, self.message = 0, ""
        self.exes, self.exe_sel, self.exe_top = [], 0, 0
        self.marked, self.queue, self.queue_total, self.queue_done, self.failed = set(), [], 0, 0, []
        self.source_root, self.draft, self.set_sel, self.pick, self.pick_sel, self.pick_top = None, None, 0, [], 0, 0
        self.field, self.browse_path, self.busy = "", "", ""
        self.pad = Gamepad()
        self.bind_keys()
        self.canvas.bind("<Button-1>", self.on_click)
        self.draw()
        self.root.after(50, self.connect)
        self.root.after(16, self.tick)

    # helpers -------------------------------------------------------------------------
    def font(self, size, bold=False):
        key = (size, bold)
        if key not in self.fonts:
            self.fonts[key] = self.tkfont.Font(family=self.family, size=-int(size * self.scale),
                                               weight="bold" if bold else "normal")
        return self.fonts[key]

    def text(self, x, y, s, size, fill=TEXT, bold=False, anchor="w"):
        self.canvas.create_text(x * self.scale, y * self.scale, text=s, fill=fill, anchor=anchor,
                                font=self.font(size, bold))

    def rect(self, x0, y0, x1, y1, fill):
        self.canvas.create_rectangle(x0 * self.scale, y0 * self.scale, x1 * self.scale, y1 * self.scale,
                                     fill=fill, width=0)

    def fit(self, s, size, bold, width):
        f = self.font(size, bold)
        if f.measure(s) <= width * self.scale:
            return s
        while s and f.measure(s + "…") > width * self.scale:
            s = s[:-1]
        return s.rstrip() + "…"

    def glyph(self, cx, cy, letter, size, fill, solid=False, letter_fill=None):
        r = size / 2 * self.scale
        x, y = cx * self.scale, cy * self.scale
        self.canvas.create_oval(x - r, y - r, x + r, y + r, outline=fill, width=2 * self.scale,
                                fill=fill if solid else "")
        self.text(cx, cy, letter, size * 0.56, letter_fill or fill, True, "center")

    def legend(self, items, y=762):
        """Footer hints, right aligned: [(letter, label, enabled, callback)]."""
        x = 1240
        for letter, label, enabled, cb in reversed(items):
            color = TEXT if enabled else DIM
            width = self.font(24).measure(label) / self.scale
            self.text(x, y, label, 24, color, False, "e")
            cx = x - width - 30
            self.glyph(cx, y, letter, 36, color)
            if enabled and cb:
                self.hits.append((cx - 24, y - 30, x, y + 30, cb))
            x = cx - 18 - 40

    def buttons(self, primary, secondary=None):
        """Large footer buttons on confirm screens."""
        self.rect(0, 680, 1280, 800, BAND)
        label, cb = primary
        w = self.font(32, True).measure(label) / self.scale + 96
        self.rect(40, 704, 40 + w, 776, FOCUS)
        self.glyph(77, 740, "A", 42, GROUND, True, FOCUS)
        self.text(110, 740, label, 32, GROUND, True)
        self.hits.append((40, 704, 40 + w, 776, cb))
        if secondary:
            label2, cb2 = secondary
            x = 40 + w + 40
            self.glyph(x + 21, 740, "B", 42, TEXT)
            self.text(x + 54, 740, label2, 32, TEXT)
            self.hits.append((x, 704, x + 96 + self.font(32).measure(label2) / self.scale, 776, cb2))

    def checkbox(self, x, cy, checked, color, mark):
        s = self.scale
        x0, y0, size = x * s, (cy - 14) * s, 28 * s
        self.canvas.create_rectangle(x0, y0, x0 + size, y0 + size, outline=color, width=2 * s,
                                     fill=color if checked else "")
        if checked:
            self.canvas.create_line(x0 + 6 * s, y0 + 15 * s, x0 + 12 * s, y0 + 21 * s, x0 + 22 * s, y0 + 7 * s,
                                    fill=FOCUS if color == GROUND else GROUND, width=3 * s)

    def header(self, right=True):
        self.rect(0, 0, 1280, 108, BAND)
        self.text(40, 54, SELF_NAME, 42, TEXT, True)
        if not right:
            return
        free, total = free_space()
        self.text(1240, 38, f"{fmt_free(free)} free", 26, TEXT, True, "e")
        self.rect(1000, 66, 1240, 74, LINE)
        self.rect(1000, 66, 1000 + 240 * (1 - free / total), 74, MUTED)

    # data ----------------------------------------------------------------------------
    def connect(self):
        def work():
            root, err = connect_source(self.cfg)
            if err:
                self.events.put(("offline", err))
                return
            try:
                self.source_root = root
                self.events.put(("games", scan_folder(root)))
            except OSError as e:
                self.events.put(("offline", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def lookup_sizes(self, games):
        """Fill in GOG's titles and sizes behind the list; a network failure just leaves sizes blank."""
        cache = load_lookups()
        for g in games:
            if g["setup"] in cache:
                self.events.put(("lookup", g["slug"], cache[g["setup"]]))
                continue
            try:
                info = gog_lookup(g["setup"])
            except (OSError, ValueError, KeyError):
                continue  # not cached, so it's tried again next time
            cache[g["setup"]] = info
            save_lookups(cache)
            self.events.put(("lookup", g["slug"], info))

    def row_kind(self, g):
        s = self.state["games"].get(g["slug"])
        if s:
            return "added" if s.get("in_steam") else "installed"
        if g["slug"] in self.marked:
            return "install"
        reserved = sum(needed_bytes(x, self.state) for x in self.games if x["slug"] in self.marked)
        return "install" if needed_bytes(g, self.state) + reserved < free_space()[0] else "nospace"

    def title_of(self, g):
        return self.state["games"].get(g["slug"], {}).get("title") or g["title"]

    def pending_count(self):
        return len(pending_games(self.state))

    # drawing -------------------------------------------------------------------------
    def draw(self):
        self.canvas.delete("all")
        self.hits = []
        getattr(self, "draw_" + self.screen)()

    def draw_connecting(self):
        self.header(False)
        self.text(40, 200, "Connecting to your server…", 42, TEXT, True)

    def draw_library(self):
        self.header()
        cols = {"installer": 840, "needs": 1010}
        self.text(40, 128, "Game", 18, MUTED, True)
        self.text(cols["installer"], 128, "Installer", 18, MUTED, True, "e")
        self.text(cols["needs"], 128, "Install size", 18, MUTED, True, "e")
        self.rect(0, 147, 1280, 149, LINE)
        n = len(self.games)
        if n > VISIBLE_ROWS:
            self.text(1240, 128, f"{self.selected + 1} of {n}", 18, MUTED, True, "e")
        if not n:
            self.text(40, 210, "No installers found", 42, TEXT, True)
            self.text(40, 262, f"Add a folder with a setup_*.exe to {' '.join(source_label(self.cfg))}", 28, MUTED)
        y = 149
        for i in range(self.top, min(self.top + VISIBLE_ROWS, n)):
            g, on = self.games[i], i == self.selected
            h = 96 if on else 78
            kind = self.row_kind(g)
            if on:
                self.rect(0, y, 1280, y + h, FOCUS)
            else:
                self.rect(0, y + h - 2, 1280, y + h, ROW_LINE)
            fg = GROUND if on else TEXT
            cy = y + h / 2
            if kind == "install":
                self.checkbox(40, cy, g["slug"] in self.marked, fg, GROUND if on else GROUND)
            self.text(84, cy, self.fit(self.title_of(g), 30, True, 560), 30, fg, True)
            self.text(cols["installer"], cy, fmt_size(g["size"]), 26, fg, False, "e")
            size = install_size(g, self.state)
            if size:
                self.text(cols["needs"], cy, fmt_size(size), 26, fg, False, "e")
            if on and kind == "install":
                chip = f"Install {len(self.marked)}" if self.marked else "Install"
                w = self.font(26, True).measure(chip) / self.scale + 88  # 12 + 36 icon + 18 gap + text + 22
                self.rect(1240 - w, cy - 28, 1240, cy + 28, CHIP)
                self.glyph(1240 - w + 30, cy, "A", 36, "#FFFFFF", True, CHIP)
                self.text(1240 - 22, cy, chip, 26, "#FFFFFF", True, "e")
            else:
                status = {"added": "In Steam", "installed": "Installed", "nospace": "Needs more space",
                          "install": ""}[kind]
                warn = kind == "nospace"
                color = (WARN_ON_FOCUS if warn else CHIP) if on else (WARN if warn else MUTED)
                self.text(1240, cy, status, 24, color, False, "e")
            self.hits.append((0, y, 1280, y + h, lambda i=i: self.tap_row(i)))
            if kind == "install":
                self.hits.append((0, y, 76, y + h, lambda i=i: self.toggle_mark(i)))
            y += h
        self.rect(0, 724, 1280, 800, BAND)
        kind = self.row_kind(self.games[self.selected]) if n else None
        if self.marked:
            label, can_a = f"Install {len(self.marked)}", True
        else:
            label = {"install": "Install", "nospace": "Not enough space"}.get(kind, "Already installed" if n else "Install")
            can_a = kind == "install"
        marked_here = bool(n) and self.games[self.selected]["slug"] in self.marked
        count = self.pending_count()
        self.legend([("A", label, can_a, self.action_a),
                     ("X", "Unselect" if marked_here else "Select", kind == "install", self.action_x),
                     ("Y", f"Add to Steam ({count})", count > 0, self.action_y),
                     ("≡", "Settings", True, self.action_menu),
                     ("B", "Clear selection" if self.marked else "Quit", True, self.action_b)])

    def draw_installing(self):
        self.header()
        progress = f"Installing {self.queue_done + 1} of {self.queue_total}" if self.queue_total > 1 else "Installing"
        self.text(40, 160, progress, 28, MUTED)
        self.text(40, 234, self.fit(self.current["title"], 84, True, 1200), 84, TEXT, True)
        y = 330
        self.rect(40, y, 1240, y + 2, LINE)
        self.text(96, y + 44, "Connected to your server", 30, MUTED)
        self.text(1240, y + 44, "Nothing copied to the Deck", 26, MUTED, False, "e")
        self.rect(40, y + 84, 1240, y + 86, LINE)
        self.rect(40, y + 86, 1240, y + 190, FOCUS)
        self.text(96, y + 138, "Running the GOG installer", 34, GROUND, True)
        self.text(1216, y + 138, "No input needed", 26, GROUND, False, "e")
        self.text(96, y + 232, "Find the game and get it ready for Steam", 30, MUTED)
        self.rect(40, y + 272, 1240, y + 274, LINE)
        self.rect(0, 724, 1280, 800, BAND)
        armed = time.monotonic() < self.stop_armed_until
        self.legend([("B", ("Press B again to stop" if armed else ("Stop installs" if self.queue_total > 1 else "Stop install")), True, self.action_b)])

    def draw_add_to_steam(self):
        pending = list(pending_games(self.state).values())
        n = len(pending)
        self.text(40, 100, f"Add {n} game{'s' if n != 1 else ''} to Steam?", 72, TEXT, True)
        y = 164
        self.rect(40, y, 1240, y + 2, LINE)
        for g in pending[:3]:
            self.text(40, y + 44, self.fit(g["title"], 34, True, 1000), 34, TEXT, True)
            self.text(1240, y + 44, fmt_size(g.get("installed_size", 0)), 26, MUTED, False, "e")
            y += 84
            self.rect(40, y, 1240, y + 2, LINE)
        if n > 3:
            self.text(40, y + 30, f"and {n - 3} more", 26, MUTED)
            y += 50
        self.text(40, y + 46, "Steam closes and reopens, which takes about 20 seconds. This app closes too.", 32)
        self.text(40, y + 90, "The games will be in your library when Steam is back.", 32)
        self.buttons(("Add and restart Steam", self.action_a), ("Not now", self.action_b))

    def draw_offline(self):
        self.header(False)
        self.text(1240, 54, "Server not connected", 22, WARN, False, "e")
        self.text(40, 200, "Can’t reach your game server", 72, TEXT, True)
        self.text(40, 280, "Check that the server is on and that the Deck is on the same network, then try again.", 32)
        where, folder = source_label(self.cfg)
        self.text(40, 350, where, 26, MUTED)
        self.text(40, 390, folder, 26, MUTED)
        self.text(40, 430, self.fit(self.message, 26, False, 1200), 26, MUTED)
        self.buttons(("Try again", self.action_a), ("Quit", self.action_b))
        self.legend([("≡", "Settings", True, self.action_menu)], 740)

    def draw_failed(self):
        self.header(False)
        names = ", ".join(g["title"] for g in self.failed)
        self.text(40, 190, self.fit(names, 28, False, 1200), 28, MUTED)
        self.text(40, 256, "The install didn’t finish" if len(self.failed) < 2 else f"{len(self.failed)} installs didn’t finish", 72, TEXT, True)
        self.text(40, 336, self.fit(self.message, 32, False, 1200), 32)
        self.text(40, 390, f"Details: {LOG_FILE.replace(HOME, '~')}", 26, MUTED)
        self.buttons(("Try again", self.action_a), ("Back to the list", self.action_b))

    def draw_pick_exe(self):
        self.header(False)
        self.text(40, 160, "Which file starts the game?", 56, TEXT, True)
        self.text(40, 216, self.current["title"], 28, MUTED)
        base = f"{prefix_dir(self.current['slug'])}/pfx/drive_c/"
        y = 256
        for i in range(self.exe_top, min(self.exe_top + 5, len(self.exes))):
            on = i == self.exe_sel
            if on:
                self.rect(0, y, 1280, y + 80, FOCUS)
            name = self.exes[i].replace(base, "")
            self.text(40, y + 40, self.fit(name, 28, on, 1200), 28, GROUND if on else TEXT, on)
            self.hits.append((0, y, 1280, y + 80, lambda i=i: self.tap_exe(i)))
            y += 80
        self.buttons(("Use this file", self.action_a), ("Cancel install", self.action_b))

    def settings_rows(self):
        d = self.draft
        rows = [("Source", "SMB share" if d["source"] == "smb" else "SSH")]
        if d["source"] == "smb":
            rows += [("Server", d["smb_server"]), ("Share", d["smb_share"]), ("Folder", d["smb_folder"] or "(top of the share)")]
        else:
            rows += [("Server", d["host"]), ("Folder", d["path"])]
        return rows

    def draw_settings(self):
        self.header(False)
        self.text(40, 160, "Settings", 56, TEXT, True)
        self.text(40, 216, "Where your GOG installers are", 28, MUTED)
        y = 256
        for i, (label, value) in enumerate(self.settings_rows()):
            editable = self.draft["source"] == "smb" or i == 0
            on = i == self.set_sel
            if on:
                self.rect(0, y, 1280, y + 84, FOCUS)
            else:
                self.rect(0, y + 82, 1280, y + 84, ROW_LINE)
            fg = GROUND if on else (TEXT if editable else MUTED)
            self.text(40, y + 42, label, 30, fg, True)
            self.text(1240, y + 42, self.fit(value, 28, False, 900), 28, fg, False, "e")
            self.hits.append((0, y, 1280, y + 84, lambda i=i: self.tap_setting(i)))
            y += 84
        if self.busy:
            self.text(40, y + 36, self.busy, 28, MUTED)
        self.buttons(("Change", self.action_a), ("Cancel", self.action_b))
        self.legend([("Y", "Save and connect", True, self.save_settings)], 740)

    def draw_edit_field(self):
        self.header(False)
        self.text(40, 160, "Server address", 56, TEXT, True)
        self.text(40, 216, "Press Steam + X for the on-screen keyboard", 28, MUTED)
        self.rect(40, 270, 1240, 360, FOCUS)
        self.text(64, 315, self.field + "_", 40, GROUND, True)
        self.buttons(("Done", self.action_a), ("Cancel", self.action_b))

    def draw_pick(self):
        self.header(False)
        title = "Choose a share" if self.screen == "pick_share" else "Choose a folder"
        self.text(40, 160, title, 56, TEXT, True)
        if self.screen == "pick_folder":
            self.text(40, 216, f"{self.draft['smb_share']}/{self.browse_path}".rstrip("/"), 28, MUTED)
        if self.busy:
            self.text(40, 300, self.busy, 32, MUTED)
        elif not self.pick:
            self.text(40, 300, "Nothing here" if self.screen == "pick_folder" else "No shares found for guests", 32, MUTED)
        y = 256
        for i in range(self.pick_top, min(self.pick_top + 5, len(self.pick))):
            on = i == self.pick_sel
            if on:
                self.rect(0, y, 1280, y + 80, FOCUS)
            self.text(40, y + 40, self.fit(self.pick[i], 30, on, 1200), 30, GROUND if on else TEXT, on)
            self.hits.append((0, y, 1280, y + 80, lambda i=i: self.tap_pick(i)))
            y += 80
        if self.screen == "pick_folder":
            self.buttons(("Open", self.action_a), ("Back", self.action_b))
            self.legend([("X", "Use this folder", True, self.action_x)], 740)
        else:
            self.buttons(("Choose", self.action_a), ("Back", self.action_b))

    def draw_pick_share(self):
        self.draw_pick()

    def draw_pick_folder(self):
        self.draw_pick()

    # settings actions ----------------------------------------------------------------
    def action_menu(self):
        if self.screen in ("library", "offline"):
            self.draft, self.set_sel, self.busy = dict(self.cfg), 0, ""
            self.go("settings")

    def save_settings(self):
        disconnect_source(self.cfg)
        self.cfg = self.draft
        save_config(self.cfg)
        self.games, self.marked = [], set()
        self.go("connecting")
        self.connect()

    def tap_setting(self, i):
        self.set_sel = i
        self.action_a()

    def tap_pick(self, i):
        if i == self.pick_sel:
            self.action_a()
        else:
            self.pick_sel = i
            self.draw()

    def load_pick(self, screen, work):
        """Fetch a share or folder list off the UI thread."""
        self.pick, self.pick_sel, self.pick_top, self.busy = [], 0, 0, "Looking…"
        self.go(screen)

        def run():
            try:
                self.events.put(("pick", screen, work()))
            except (OSError, subprocess.SubprocessError):
                self.events.put(("pick", screen, []))
        threading.Thread(target=run, daemon=True).start()

    def open_folder(self, path):
        self.browse_path = path
        server, share = self.draft["smb_server"], self.draft["smb_share"]
        self.load_pick("pick_folder", lambda: smb_folders(server, share, path))

    def settings_change(self):
        label = self.settings_rows()[self.set_sel][0]
        d = self.draft
        if label == "Source":
            d["source"] = "ssh" if d["source"] == "smb" else "smb"
            self.draw()
        elif d["source"] != "smb":
            return
        elif label == "Server":
            self.field = d["smb_server"]
            self.go("edit_field")
        elif label == "Share":
            server = d["smb_server"]
            self.load_pick("pick_share", lambda: smb_shares(server))
        elif label == "Folder":
            self.open_folder(d["smb_folder"].strip("/"))

    def on_key(self, e):
        if self.screen != "edit_field":
            return
        if e.keysym == "BackSpace":
            self.field = self.field[:-1]
        elif e.char and e.char.isprintable() and e.keysym not in ("Return", "Escape"):
            self.field += e.char
        self.draw()

    # actions -------------------------------------------------------------------------
    def go(self, screen):
        self.screen = screen
        self.draw()

    def tap_row(self, i):
        if i == self.selected:
            self.action_a()
        else:
            self.selected = i
            self.draw()

    def toggle_mark(self, i=None):
        i = self.selected if i is None else i
        if not self.games:
            return
        g = self.games[i]
        if g["slug"] in self.marked:
            self.marked.discard(g["slug"])
        elif self.row_kind(g) == "install":
            self.marked.add(g["slug"])
        self.draw()

    def tap_exe(self, i):
        self.exe_sel = i
        self.draw()

    def move(self, step):
        if self.screen == "settings":
            self.set_sel = max(0, min(len(self.settings_rows()) - 1, self.set_sel + step))
            self.draw()
        elif self.screen in ("pick_share", "pick_folder") and self.pick:
            self.pick_sel = max(0, min(len(self.pick) - 1, self.pick_sel + step))
            if self.pick_sel < self.pick_top:
                self.pick_top = self.pick_sel
            elif self.pick_sel >= self.pick_top + 5:
                self.pick_top = self.pick_sel - 4
            self.draw()
        elif self.screen == "library" and self.games:
            self.selected = max(0, min(len(self.games) - 1, self.selected + step))
            if self.selected < self.top:
                self.top = self.selected
            elif self.selected >= self.top + VISIBLE_ROWS:
                self.top = self.selected - VISIBLE_ROWS + 1
            self.draw()
        elif self.screen == "pick_exe" and self.exes:
            self.exe_sel = max(0, min(len(self.exes) - 1, self.exe_sel + step))
            if self.exe_sel < self.exe_top:
                self.exe_top = self.exe_sel
            elif self.exe_sel >= self.exe_top + 5:
                self.exe_top = self.exe_sel - 4
            self.draw()

    def start_queue(self, games):
        self.queue, self.queue_total, self.queue_done, self.failed = list(games), len(games), 0, []
        self.marked = set()
        self.next_install()

    def next_install(self):
        """One installer at a time; when the queue is empty, offer the next step."""
        if self.queue:
            self.current = self.queue.pop(0)
            self.stop_armed_until = 0
            self.job = InstallJob(self.current, self.events, self.source_root)
            self.job.start()
            self.go("installing")
            return
        self.job = None
        if self.failed:
            self.go("failed")
        elif self.pending_count():
            self.go("add_to_steam")
        else:
            self.go("library")

    def install_finished(self, failed=False):
        self.queue_done += 1
        if failed:
            self.failed.append(self.current)
        self.next_install()

    def action_a(self):
        s = self.screen
        if s == "settings":
            self.settings_change()
        elif s == "edit_field":
            self.draft["smb_server"] = self.field.strip()
            self.go("settings")
        elif s == "pick_share":
            if self.pick:
                self.draft["smb_share"], self.draft["smb_folder"] = self.pick[self.pick_sel], ""
                self.go("settings")
        elif s == "pick_folder":
            if self.pick:
                self.open_folder(f"{self.browse_path}/{self.pick[self.pick_sel]}".strip("/"))
        elif s == "library":
            if self.marked:
                self.start_queue([g for g in self.games if g["slug"] in self.marked])
            elif self.games and self.row_kind(self.games[self.selected]) == "install":
                self.start_queue([self.games[self.selected]])
        elif s == "add_to_steam":
            start_apply_steam()
            self.quit()
        elif s == "offline":
            self.go("connecting")
            self.connect()
        elif s == "failed":
            self.start_queue(self.failed)
        elif s == "pick_exe":
            exe = self.exes[self.exe_sel]
            record_install(self.state, self.current,
                           {"exe": exe, "workdir": os.path.dirname(exe), "args": "", "root": os.path.dirname(exe)})
            self.install_finished()

    def action_b(self):
        s = self.screen
        if s == "settings":
            self.go("library" if self.source_root else "offline")
        elif s in ("edit_field", "pick_share"):
            self.go("settings")
        elif s == "pick_folder":
            if self.browse_path:
                self.open_folder(os.path.dirname(self.browse_path))
            else:
                self.go("settings")
        elif s == "library" and self.marked:
            self.marked = set()
            self.draw()
        elif s == "library" or s == "offline":
            self.quit()
        elif s == "add_to_steam":
            self.go("library")
        elif s == "failed":
            self.go("library")
        elif s == "installing":
            # a stray press must not end a long install, so stopping takes two presses
            if time.monotonic() < self.stop_armed_until:
                self.job.stop()
            else:
                self.stop_armed_until = time.monotonic() + 4
                self.root.after(4100, lambda: self.screen == "installing" and self.draw())
            self.draw()
        elif s == "pick_exe":
            discard_prefix(self.current["slug"], self.state)
            self.install_finished()

    def action_x(self):
        if self.screen == "library":
            self.toggle_mark()
        elif self.screen == "pick_folder":
            self.draft["smb_folder"] = self.browse_path
            self.go("settings")

    def action_y(self):
        if self.screen == "settings":
            self.save_settings()
        elif self.screen == "library" and self.pending_count():
            self.go("add_to_steam")

    def quit(self):
        if self.job and self.job.is_alive():
            self.job.stop()
            self.job.join(8)
        disconnect_source(self.cfg)
        self.root.destroy()

    def bind_keys(self):
        self.root.bind("<Up>", lambda e: self.move(-1))
        self.root.bind("<Down>", lambda e: self.move(1))
        self.root.bind("<Return>", lambda e: self.action_a())
        self.root.bind("<Escape>", lambda e: self.action_b())
        # letters go to the text field while it's open, so these only act elsewhere
        self.root.bind("x", lambda e: self.on_key(e) if self.screen == "edit_field" else self.action_x())
        self.root.bind("y", lambda e: self.on_key(e) if self.screen == "edit_field" else self.action_y())
        self.root.bind("<Key>", self.on_key, add="+")

    def on_click(self, e):
        x, y = e.x / self.scale, e.y / self.scale
        for x0, y0, x1, y1, cb in reversed(self.hits):
            if x0 <= x <= x1 and y0 <= y <= y1:
                cb()
                return

    def tick(self):
        for action in self.pad.poll():
            {"up": lambda: self.move(-1), "down": lambda: self.move(1), "a": self.action_a,
             "b": self.action_b, "x": self.action_x, "y": self.action_y, "menu": self.action_menu}[action]()
        try:
            while True:
                self.on_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(16, self.tick)

    def on_event(self, ev):
        kind = ev[0]
        if kind == "games":
            self.games = ev[1]
            self.selected = self.top = 0
            self.go("library")
            threading.Thread(target=self.lookup_sizes, args=(self.games,), daemon=True).start()
        elif kind == "pick":
            if self.screen == ev[1]:
                self.pick, self.busy = ev[2], ""
                self.draw()
        elif kind == "lookup":
            g = next((x for x in self.games if x["slug"] == ev[1]), None)
            if g:
                apply_lookup(g, ev[2])
                if self.screen == "library":
                    self.draw()
        elif kind == "offline":
            self.message = ev[1]
            self.go("offline")
        elif kind == "stopped":
            discard_prefix(self.current["slug"], self.state)
            self.queue, self.queue_total = [], 0
            self.go("library")
        elif kind == "error":
            self.message = ev[1]
            discard_prefix(self.current["slug"], self.state)
            self.install_finished(failed=True)
        elif kind == "done":
            task, exes = ev[1], ev[2]
            if task:
                record_install(self.state, self.current, task)
                self.install_finished()
            elif exes:
                self.exes, self.exe_sel, self.exe_top = exes, 0, 0
                self.go("pick_exe")
            else:
                self.message = "The installer closed before the game was installed."
                discard_prefix(self.current["slug"], self.state)
                self.install_finished(failed=True)

    def run(self):
        self.root.mainloop()


def main():
    if "--apply-steam" in sys.argv:
        apply_steam()
    elif "--fetch-art" in sys.argv:
        fetch_all_art(load_state())
    elif "--list" in sys.argv:
        cfg = load_config()
        root, err = connect_source(cfg)
        if err:
            sys.exit(f"Can't reach the server: {err}")
        try:
            for g in scan_folder(root):
                apply_lookup(g, gog_lookup(g["setup"]))
                size = fmt_size(g["install_size"]) if g.get("install_size") else "size unknown"
                print(f"{g['title']} | installer {fmt_size(g['size'])} | install {size} | gog {g.get('gog_id') or 'no match'} | "
                      f"{g['setup']}")
        finally:
            disconnect_source(cfg)
    else:
        App().run()


if __name__ == "__main__":
    main()
