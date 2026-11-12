#!/usr/bin/env python3
"""GOG Backup Installer for Steam Deck: a fullscreen, gamepad-first Steam Deck app.

Lists GOG installers on an SMB guest share or a USB drive (both read-only), runs the chosen one
under Proton without copying it to the Deck, and adds the finished games to Steam.
Needs only what SteamOS ships: python3 with tkinter and evdev, libsmbclient, and Steam's Proton.

Usage: gog_installer.py                 run the app
       gog_installer.py --list          print what each source holds, then exit
       gog_installer.py --apply-steam   add pending games to Steam (restarts Steam)
       gog_installer.py --fetch-art     download missing artwork; Steam shows it after its next restart
       gog_installer.py --update-protondb [--force]   refresh ProtonDB fix suggestions (monthly export)
       gog_installer.py --smbfs URL DIR mount a guest SMB folder read-only (the app starts this itself)
"""
import base64
import binascii
import codecs
import errno
import glob
import html
import json
import os
import queue
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
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
STEAM = f"{HOME}/.local/share/Steam"
COMMON = f"{STEAM}/steamapps/common"
COMPAT_TOOL = ("Proton - Experimental", "proton_experimental")
SELF_NAME = "GOG Backup Installer"
SILENT_FLAGS = ["/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART"]  # Inno Setup: progress window, no questions
LOOKUP_FILE = f"{DATA}/gog-lookup-v2.json"
INSTALLER_INFO_FILE = f"{DATA}/installer-info.json"

CACHE = f"{HOME}/.cache/gog-installer"
SOURCE_TYPES = ("smb", "usb")


def migrate_config(cfg):
    """Older versions kept one SMB source in flat keys, and had SSH sources; now a list of SMB and USB sources."""
    if isinstance(cfg.get("sources"), list):
        return {"sources": [src for src in cfg["sources"] if src.get("type") in SOURCE_TYPES]}
    if cfg.get("source") == "smb":
        return {"sources": [{"type": "smb", "server": cfg.get("smb_server", ""), "share": cfg.get("smb_share", ""),
                             "folder": cfg.get("smb_folder", "")}]}
    return {"sources": []}


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    return migrate_config(cfg)


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


# --- SMB guest shares --------------------------------------------------------------------
# Guest shares: listed with smbclient, mounted read-only by SmbFs below. Neither needs root.

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


def smb_url(server, share, folder, path=""):
    """smb://server/share/folder/path, percent-encoded; libsmbclient decodes it."""
    parts = [p for p in f"{share}/{folder}/{path}".split("/") if p]
    return f"smb://{server}/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)


# Layouts on x86_64 glibc, which is what SteamOS runs.
STAT_SIZE = 144  # struct stat
DIRENT_NAME = 28  # offsetof(struct smbc_dirent, name): type, dirlen, commentlen, pad, comment ptr, namelen
SMBC_DIR, SMBC_FILE = 7, 8


def parse_stat(buf):
    """(is_dir, size, mtime) from a struct stat."""
    mode = struct.unpack_from("<I", buf, 24)[0]
    return (mode & 0o170000) == 0o040000, struct.unpack_from("<q", buf, 48)[0], struct.unpack_from("<q", buf, 88)[0]


def build_stat(is_dir, size, mtime):
    """A read-only struct stat for FUSE: everything readable, nothing writable."""
    buf = bytearray(STAT_SIZE)
    struct.pack_into("<Q", buf, 16, 2 if is_dir else 1)  # st_nlink
    struct.pack_into("<III", buf, 24, (0o040555 if is_dir else 0o100444), os.getuid(), os.getgid())
    struct.pack_into("<qqq", buf, 48, size, 4096, (size + 511) // 512)  # st_size, st_blksize, st_blocks
    for off in (72, 88, 104):  # st_atim, st_mtim, st_ctim
        struct.pack_into("<q", buf, off, mtime)
    return bytes(buf)


class SmbFs:
    """Read-only FUSE filesystem over a guest SMB share, using libsmbclient and libfuse 2 from SteamOS.
    Runs single-threaded in its own process: python3 gog_installer.py --smbfs URL MOUNTPOINT"""

    def __init__(self, url):
        import ctypes
        c = self.c = ctypes
        self.base = url.rstrip("/")
        self.smb = c.CDLL("libsmbclient.so.0", use_errno=True)
        self.fuse = c.CDLL("libfuse.so.2", use_errno=True)
        auth_t = c.CFUNCTYPE(None, c.c_char_p, c.c_char_p, c.c_void_p, c.c_int, c.c_void_p, c.c_int,
                             c.c_void_p, c.c_int)

        def guest(server, share, wg, wglen, user, userlen, pw, pwlen):
            c.memset(user, 0, userlen)
            c.memset(pw, 0, pwlen)
        self._auth = auth_t(guest)
        s = self.smb
        s.smbc_init.argtypes, s.smbc_init.restype = [auth_t, c.c_int], c.c_int
        s.smbc_stat.argtypes, s.smbc_stat.restype = [c.c_char_p, c.c_void_p], c.c_int
        s.smbc_opendir.argtypes, s.smbc_opendir.restype = [c.c_char_p], c.c_int
        s.smbc_readdir.argtypes, s.smbc_readdir.restype = [c.c_uint], c.c_void_p
        s.smbc_closedir.argtypes, s.smbc_closedir.restype = [c.c_int], c.c_int
        s.smbc_open.argtypes, s.smbc_open.restype = [c.c_char_p, c.c_int, c.c_ushort], c.c_int
        s.smbc_lseek.argtypes, s.smbc_lseek.restype = [c.c_int, c.c_longlong, c.c_int], c.c_longlong
        s.smbc_read.argtypes, s.smbc_read.restype = [c.c_int, c.c_void_p, c.c_size_t], c.c_ssize_t
        s.smbc_close.argtypes, s.smbc_close.restype = [c.c_int], c.c_int
        if s.smbc_init(self._auth, 0) < 0:
            raise OSError(c.get_errno(), "libsmbclient didn't start")

    def url(self, path):
        parts = [p for p in path.decode("utf-8", "surrogateescape").split("/") if p]
        return (self.base + "".join("/" + urllib.parse.quote(p, safe="") for p in parts)).encode()

    def err(self):
        return -(self.c.get_errno() or errno.EIO)

    # FUSE operations: each returns 0 or a negative errno ------------------------------------
    def getattr(self, path, st):
        buf = self.c.create_string_buffer(STAT_SIZE)
        if self.smb.smbc_stat(self.url(path), buf) < 0:
            return self.err()
        self.c.memmove(st, build_stat(*parse_stat(buf.raw)), STAT_SIZE)
        return 0

    def readdir(self, path, buf, filler, offset, fi):
        dh = self.smb.smbc_opendir(self.url(path))
        if dh < 0:
            return self.err()
        try:
            for name in (b".", b".."):
                filler(buf, name, None, 0)
            while True:
                ent = self.smb.smbc_readdir(dh)
                if not ent:
                    break
                kind = self.c.c_uint.from_address(ent).value
                name = self.c.string_at(ent + DIRENT_NAME)
                if kind in (SMBC_DIR, SMBC_FILE) and name not in (b".", b".."):
                    filler(buf, name, None, 0)
        finally:
            self.smb.smbc_closedir(dh)
        return 0

    def open(self, path, fi):
        if self.c.c_int.from_address(fi).value & 3 != os.O_RDONLY:
            return -errno.EROFS
        fd = self.smb.smbc_open(self.url(path), os.O_RDONLY, 0)
        if fd < 0:
            return self.err()
        self.c.c_uint64.from_address(fi + 24).value = fd  # fuse_file_info.fh
        return 0

    def read(self, path, buf, size, offset, fi):
        fd = self.c.c_uint64.from_address(fi + 24).value
        if self.smb.smbc_lseek(fd, offset, 0) < 0:
            return self.err()
        done = 0
        while done < size:  # FUSE treats a short read as the end of the file
            n = self.smb.smbc_read(fd, buf + done, size - done)
            if n < 0:
                return self.err()
            if n == 0:
                break
            done += n
        return done

    def release(self, path, fi):
        self.smb.smbc_close(self.c.c_uint64.from_address(fi + 24).value)
        return 0

    def run(self, mountpoint, label):
        c = self.c
        filler_t = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_char_p, c.c_void_p, c.c_longlong)
        types = {
            "getattr": c.CFUNCTYPE(c.c_int, c.c_char_p, c.c_void_p),
            "open": c.CFUNCTYPE(c.c_int, c.c_char_p, c.c_void_p),
            "read": c.CFUNCTYPE(c.c_int, c.c_char_p, c.c_void_p, c.c_size_t, c.c_longlong, c.c_void_p),
            "release": c.CFUNCTYPE(c.c_int, c.c_char_p, c.c_void_p),
            "readdir": c.CFUNCTYPE(c.c_int, c.c_char_p, c.c_void_p, filler_t, c.c_longlong, c.c_void_p),
        }
        # struct fuse_operations from libfuse 2.9, in order
        names = ["getattr", "readlink", "getdir", "mknod", "mkdir", "unlink", "rmdir", "symlink", "rename", "link",
                 "chmod", "chown", "truncate", "utime", "open", "read", "write", "statfs", "flush", "release",
                 "fsync", "setxattr", "getxattr", "listxattr", "removexattr", "opendir", "readdir", "releasedir",
                 "fsyncdir", "init", "destroy", "access", "create", "ftruncate", "fgetattr", "lock", "utimens",
                 "bmap", "flags", "ioctl", "poll", "write_buf", "read_buf", "flock", "fallocate"]
        fields = [(n, c.c_uint if n == "flags" else types.get(n, c.c_void_p)) for n in names]
        Ops = type("FuseOperations", (c.Structure,), {"_fields_": fields})

        def safe(fn):
            def call(*args):
                try:
                    return fn(*args)
                except Exception:
                    return -errno.EIO
            return call
        self._ops = Ops(**{n: t(safe(getattr(self, n))) for n, t in types.items()})
        argv = [b"gog-smbfs", mountpoint.encode(), b"-f", b"-s", b"-o",
                f"ro,fsname={label},subtype=gog-smb".encode()]
        arr = (c.c_char_p * len(argv))(*argv)
        self.fuse.fuse_main_real.argtypes = [c.c_int, c.c_void_p, c.c_void_p, c.c_size_t, c.c_void_p]
        return self.fuse.fuse_main_real(len(argv), arr, c.byref(self._ops), c.sizeof(Ops), None)


def mount_smb(src, mp):
    """Start the read-only SMB filesystem for a source's folder at mp. Returns (path, error)."""
    url = smb_url(src["server"], src["share"], src["folder"])
    log_path = f"{DATA}/smbfs-{os.path.basename(mp)}.log"
    if os.path.ismount(mp):
        try:
            os.listdir(mp)
            return mp, None
        except OSError:
            unmount_fuse(mp)
    os.makedirs(mp, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)
    with open(log_path, "w") as log:
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--smbfs", url, mp],
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(100):
        if os.path.ismount(mp):
            return mp, None
        if proc.poll() is not None:
            break
        time.sleep(0.15)
    if proc.poll() is None:
        proc.terminate()
    try:
        with open(log_path) as f:
            detail = f.read().strip().splitlines()[-1:]
    except OSError:
        detail = []
    return None, (detail[0] if detail else f"Couldn't open {url}")


def unmount_fuse(mp):
    for tool in ("fusermount", "fusermount3"):
        if subprocess.run([tool, "-uz", mp], capture_output=True).returncode == 0:
            return


# --- USB drives and SD cards ---------------------------------------------------------------
# Game Mode only automounts ext4 (for Steam libraries). Other drives, such as exFAT, are mounted
# read-only through udisks, which lets the user at the Deck do that without a password.

DRIVE_FS = ("exfat", "vfat", "ntfs", "ntfs3", "ext4", "btrfs", "f2fs")
_mounted_by_us = set()


def parse_drives(lsblk_json):
    """Removable partitions with a filesystem: [{"dev", "uuid", "label", "fstype", "size", "mountpoint"}]."""
    devices = json.loads(lsblk_json).get("blockdevices", [])
    by_name = {d.get("path"): d for d in devices}
    truthy = lambda v: v in (True, 1, "1", "true")
    out = []
    for d in devices:
        if d.get("type") != "part" or (d.get("fstype") or "") not in DRIVE_FS or not d.get("uuid"):
            continue
        parent = by_name.get(f"/dev/{d.get('pkname')}", {})
        removable = truthy(d.get("hotplug")) or truthy(parent.get("hotplug")) or \
            parent.get("tran") in ("usb", "mmc") or (d.get("path") or "").startswith("/dev/mmcblk")
        if removable:
            out.append({"dev": d["path"], "uuid": d["uuid"], "label": d.get("label") or "",
                        "fstype": d["fstype"], "size": int(d.get("size") or 0), "mountpoint": d.get("mountpoint")})
    return out


def list_drives():
    r = subprocess.run(["lsblk", "--json", "--list", "--bytes", "-o",
                        "PATH,PKNAME,TYPE,UUID,LABEL,FSTYPE,TRAN,HOTPLUG,SIZE,MOUNTPOINT"],
                       capture_output=True, text=True, timeout=10)
    return parse_drives(r.stdout) if r.returncode == 0 else []


def drive_name(d):
    return d.get("label") or f"{fmt_size(d.get('size', 0))} drive"


def parse_udisks_mount(output):
    m = re.search(r"Mounted \S+ at (.+?)\.?\s*$", output.strip())
    return m.group(1) if m else None


def mount_usb(src):
    """The drive's folder, mounted read-only unless it already is mounted. Returns (path, error)."""
    drive = next((d for d in list_drives() if d["uuid"] == src.get("uuid")), None)
    if not drive:
        return None, f"{src.get('label') or 'The USB drive'} isn't plugged in"
    mp = drive["mountpoint"]
    if not mp:
        r = subprocess.run(["udisksctl", "mount", "--no-user-interaction", "-b", drive["dev"], "-o", "ro"],
                           capture_output=True, text=True, timeout=30)
        mp = parse_udisks_mount(r.stdout)
        if not mp:
            return None, (r.stderr.strip() or r.stdout.strip() or "Couldn't mount the drive")
        _mounted_by_us.add(drive["dev"])
    return os.path.join(mp, src.get("folder", "").strip("/")), None


def unmount_usb(src):
    for d in list_drives():
        if d["uuid"] == src.get("uuid") and d["dev"] in _mounted_by_us:
            subprocess.run(["udisksctl", "unmount", "--no-user-interaction", "-b", d["dev"]], capture_output=True)
            _mounted_by_us.discard(d["dev"])


# --- sources -------------------------------------------------------------------------------

def source_mount_dir(src):
    """A mount folder per source, stable for its settings."""
    key = json.dumps({k: src.get(k) for k in ("type", "server", "share", "folder")}, sort_keys=True)
    return f"{CACHE}/{src['type']}-{binascii.crc32(key.encode()):08x}"


def connect_source(src):
    """Make a source's installer folder available. Returns (folder path, error)."""
    try:
        if src["type"] == "smb":
            return mount_smb(src, source_mount_dir(src))
        if src["type"] == "usb":
            return mount_usb(src)
        return None, "Unknown source type"
    except (OSError, subprocess.SubprocessError, KeyError) as e:
        return None, str(e)


def disconnect_source(src):
    try:
        if src["type"] == "usb":
            unmount_usb(src)
        else:
            unmount_fuse(source_mount_dir(src))
    except (OSError, subprocess.SubprocessError, KeyError):
        pass


def is_complete(src):
    """Enough to connect: a share for SMB, a drive for USB."""
    if src.get("type") == "smb":
        return bool(src.get("server") and src.get("share"))
    if src.get("type") == "usb":
        return bool(src.get("uuid"))
    return False


def source_label(src):
    """(kind, where, folder) for showing a source."""
    t = src.get("type")
    if t == "smb":
        return "SMB", src.get("server") or "(no server)", f"{src.get('share', '')}/{src.get('folder', '')}".strip("/")
    return "USB", src.get("label") or "(no drive)", src.get("folder", "").strip("/") or "(top of the drive)"


SOURCE_PREFERENCE = {"usb": 0, "smb": 1}  # local drive first: fastest, and works offline


def title_key(title):
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def installed_slug(game, folders, installed):
    """The state key of an earlier install of this game, or None.

    New installs record the game id. Older ones were keyed by their folder name, or recorded the setup
    file; the oldest are matched by title. The first rule with exactly one hit wins."""
    if not isinstance(installed, dict):
        installed = {k: {} for k in installed}
    rules = [
        lambda k, v: v.get("game_id") == game["id"],
        lambda k, v: k == game["id"] or k in folders,
        lambda k, v: bool(v.get("setup")) and game_id(v["setup"]) == game["id"],
        lambda k, v: not v.get("setup") and not v.get("game_id") and bool(v.get("title"))
        and title_key(v["title"]) in {title_key(t) for t in (game["title"], game.get("product_name") or "",
                                                              clean_title(game["setup"]))} - {""},
    ]
    for rule in rules:
        hits = sorted(k for k, v in installed.items() if rule(k, v))
        if len(hits) == 1:
            return hits[0]
        if hits:  # two installs of one game: the one in a folder of this name, else the first
            return next((k for k in hits if k in folders), hits[0])
    return None


def version_key(version):
    """For picking the newest build: numbers compare as numbers, "1.10" after "1.9"."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.findall(r"\d+|[a-z]+", (version or "").lower())]


def merge_games(per_source, installed=()):
    """One row per game, whichever folders and sources hold it.

    A game is its installer (setup_<game>_<version>.exe), never its folder name. When several sources have
    it, the newest build is listed; the same build on several sources is read from the preferred one (USB).
    An earlier install keeps its identity, so its Steam entry, prefix and saves are found again."""
    groups = {}
    for i, (src, games) in enumerate(per_source):
        for g in games:
            groups.setdefault(g["id"], []).append((i, src, g))
    out = []
    for group in groups.values():
        order, src, best = max(group, key=lambda e: (version_key(e[2].get("version")),
                                                     -SOURCE_PREFERENCE.get(e[1].get("type"), 9), -e[0]))
        folders = {g["folder"] for _, _, g in group}
        slug = installed_slug(best, folders, installed) or best["id"]
        out.append(dict(best, slug=slug, source=source_label(src)[0]))
    return sorted(out, key=lambda g: g["title"].lower())


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


def game_id(setup):
    """setup_hollow_knight_1.5.12620_(64bit)_(89718).exe -> "hollow_knight": the same for every build."""
    return parse_setup(setup)[0].lower()


def pe_resources(f):
    """The .rsrc section of an open PE file (about 100 KB for a GOG installer), or b""."""
    head = f.read(4096)
    if head[:2] != b"MZ" or len(head) < 0x40:
        return b""
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if pe + 24 > len(head) or head[pe:pe + 4] != b"PE\0\0":
        return b""
    nsec, osz = struct.unpack_from("<H", head, pe + 6)[0], struct.unpack_from("<H", head, pe + 20)[0]
    for i in range(nsec):
        at = pe + 24 + osz + i * 40
        if at + 40 > len(head):
            break
        name, _, _, rsize, raw = struct.unpack_from("<8sIIII", head, at)
        if name.rstrip(b"\0") == b".rsrc":
            f.seek(raw)
            return f.read(min(rsize, 8 << 20))
    return b""


def version_value(key, data):
    """A version-info string: the UTF-16 key, padding, then the UTF-16 value up to its terminating zero."""
    needle = (key + "\0").encode("utf-16-le")
    i = data.find(needle)
    if i < 0:
        return None
    i += len(needle)
    while i + 1 < len(data) and data[i:i + 2] == b"\0\0":
        i += 2
    j = i
    while j + 1 < len(data) and data[j:j + 2] != b"\0\0" and j - i < 600:
        j += 2
    text = data[i:j].decode("utf-16-le", "replace").strip()
    return text or None


def parse_version_info(data):
    """{"name", "version"} from an installer's resources: "The Witcher 3 - Wild Hunt - Complete Edition", "4.04b"."""
    name = version_value("ProductName", data)
    version = version_value("ProductVersion", data)
    if version:  # GOG appends a build id: "4.04b.[57606063263997132]"
        version = re.sub(r"\.?\[[^\]]*\]$", "", version) or None
    return {"name": clean_store_title(name) if name else None, "version": version}


NAME_CUT = 60  # GOG's installers cut long product names at 60 characters ("… Game of the Year Edition D")


def name_is_cut(name):
    return len(name or "") >= NAME_CUT


def whole_words(name):
    """A cut name back to its last whole word: "… Game of the Year Edition D" -> "… Game of the Year Edition"."""
    return re.sub(r"[\s\-:–—]+$", "", name.rsplit(" ", 1)[0]) if " " in name else name


def installer_info(path, cache=None):
    """The installer's own name and version, read once per file (by name and size)."""
    key = f"{os.path.basename(path)}|{os.path.getsize(path)}"
    if cache is not None and key in cache:
        return cache[key]
    try:
        with open(path, "rb") as f:
            info = parse_version_info(pe_resources(f))
    except (OSError, struct.error):
        info = {"name": None, "version": None}
    if cache is not None:
        cache[key] = info
    return info


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def scan_folder(root):
    """One entry per game folder that holds a setup_*.exe directly inside it (Extras is ignored).

    The game is identified by its installer: the setup file's name gives the id, its version info the
    title and version. The folder's name is only where it is on this source."""
    games = []
    cache = load_json(INSTALLER_INFO_FILE)
    before = len(cache)
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
        info = installer_info(os.path.join(folder, setup), cache)
        product = info.get("name")
        # the installer's own name first: GOG renames games in its store ("Remastered" isn't the 4.04 build)
        title = (product if not name_is_cut(product) else whole_words(product)) if product else clean_title(setup)
        games.append({"id": game_id(setup), "slug": game_id(setup), "folder": name, "title": title,
                      "product_name": product, "version": info.get("version") or parse_setup(setup)[1],
                      "setup": setup, "size": size})
    if len(cache) != before:
        try:
            save_json(INSTALLER_INFO_FILE, cache)
        except OSError:
            pass
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


def match_catalog(names, products):
    """The product whose slug or title is exactly one of the names (ignoring case, spaces and punctuation)."""
    for name in names:
        want = normalize_title(name)
        if not want:
            continue
        for p in products:
            if want in (normalize_title(p.get("slug") or ""), normalize_title(p.get("title") or "")):
                return p
    return None


def base_title(title):
    """"The Witcher 3: Wild Hunt — Remastered" -> "The Witcher 3: Wild Hunt"; also cuts at " - " and " – "."""
    for sep in (" — ", " – ", " - "):
        title = title.split(sep)[0]
    return title.strip()


def main_title(title):
    """"Gabriel Knight 3: Blood of the Sacred" -> "Gabriel Knight 3"."""
    return base_title(title).split(":")[0].strip()


def loose_match(title, wanted):
    """A store title whose part before " - " or ":" is exactly one of the wanted names."""
    return normalize_title(base_title(title)) in wanted or normalize_title(main_title(title)) in wanted


def single(candidates):
    """The only candidate, or None: "Fallout" loosely fits both "Fallout: New Vegas" and "Fallout 2:…"."""
    return candidates[0] if len(candidates) == 1 else None


def search_terms(name):
    """The full name, the part before the first " - " / ":" / "—", and the first four words."""
    clean = name.replace("_", " ").strip()
    terms = [clean]
    if base_title(clean) != clean:
        terms.append(base_title(clean))
    words = [w for w in clean.split() if w != "-"]
    if len(words) > 4:
        terms.append(" ".join(words[:4]))
    return terms


def vertical_cover(url):
    """GOG image URLs take a size suffix; the Galaxy vertical cover is the portrait box art (342x482)."""
    if not url or "_glx_vertical_cover" in url:
        return url
    stem, dot, ext = url.rpartition(".")
    return f"{stem}_glx_vertical_cover.{ext}" if dot and len(ext) <= 4 else url


def catalog_match(term, names, relaxed=False):
    """GOG's store catalog: an exact slug/title match, or (relaxed) the single loose match."""
    q = urllib.parse.quote(term)
    found = decode_json(http_get(f"https://catalog.gog.com/v1/catalog?limit=8&query=like:{q}"
                                 "&order=desc:score&productType=in:game,pack"))
    products = found.get("products", [])
    wanted = {normalize_title(n) for n in names}
    p = single([x for x in products if loose_match(x.get("title") or "", wanted)]) if relaxed \
        else match_catalog(names, products)
    if not p:
        return None
    return {"id": str(p["id"]), "title": clean_store_title(p.get("title") or term),
            "cover_vertical": vertical_cover(p.get("coverVertical")), "cover_horizontal": p.get("coverHorizontal"),
            "logo": p.get("logo")}


GOGDB_ROW = re.compile(r'<td class="col-id">\s*<a[^>]*href="/product/(\d+)"[^>]*>.*?'
                       r'<td class="col-name[^"]*">\s*<a[^>]*>\s*(.*?)\s*</a>', re.S)


def parse_gogdb(page):
    return [(pid, clean_store_title(html.unescape(re.sub(r"<[^>]+>", "", title))))
            for pid, title in GOGDB_ROW.findall(page)]


def gogdb_match(term, names, relaxed=False):
    """A GOG product id from GOG Database's search (it lists delisted games too)."""
    page = http_get(f"https://www.gogdb.org/products?search={urllib.parse.quote(term)}").decode("utf-8", "replace")
    rows = parse_gogdb(page)
    wanted = {normalize_title(n) for n in names}
    if relaxed:
        hit = single([r for r in rows if loose_match(r[1], wanted)])
        return hit[0] if hit else None
    return next((pid for pid, title in rows if normalize_title(title) in wanted), None)


def game_by_id(pid):
    """Title and art by GOG product id, from the API GOG Galaxy uses. Works for delisted games."""
    root = json.loads(http_get(f"https://api.gog.com/v2/games/{pid}?locale=en-US"))
    links = root.get("_links", {})
    product = root.get("_embedded", {}).get("product", {})
    href = lambda k: (links.get(k) or {}).get("href")
    return {"id": str(pid), "title": clean_store_title(product.get("title") or ""),
            "cover_vertical": vertical_cover(href("boxArtImage")), "cover_horizontal": href("backgroundImage"),
            "logo": href("logo")}


def steam_search(term):
    q = urllib.parse.quote(term)
    data = json.loads(http_get(f"https://store.steampowered.com/api/storesearch/?term={q}&l=english&cc=US"))
    return [i for i in data.get("items", []) if i.get("type") == "app"]


def steam_match(titles, gog_title=None):
    """Steam app id whose name exactly matches one of the titles, for its 600x900 cover and ProtonDB.

    Editions share the base game's page, so GOG's title may lose an edition suffix ("… — Remastered");
    the installer's name never does ("Baldur's Gate - The Original Saga" isn't "Baldur's Gate").
    Only when nothing matches exactly, a single Steam name whose part before " - " is a full title."""
    titles = [t for t in titles if t and normalize_title(t)]
    wanted = {normalize_title(t) for t in titles + ([base_title(gog_title)] if gog_title else [])}
    full = {normalize_title(t) for t in titles}
    terms = []
    for t in titles:
        short = re.split(r"[:–]", t)[0].split(" - ")[0].strip()
        for term in (t, short):
            if term and term.lower() not in [x.lower() for x in terms]:
                terms.append(term)
    results = {}
    for term in terms:
        try:
            results[term] = steam_search(term)
        except (OSError, ValueError):
            results[term] = []
        hit = next((a["id"] for a in results[term] if normalize_title(a.get("name") or "") in wanted), None)
        if hit:
            return hit
    for term in terms:  # Steam names often add a colon subtitle that is another game: only " - " may go
        hit = single([a for a in results[term] if normalize_title(base_title(a.get("name") or "")) in full])
        if hit:
            return hit["id"]
    return None


def match_build(version, items):
    want = normalize_title(version or "")
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


def gog_lookup(setup, product_name=None, version=None):
    """What GOG knows about an installer: {"id", "title", "install_size", "steam_appid", "cover_vertical",
    "cover_horizontal", "logo"}, or {} when nothing matches. Same rules as GOG Disc Archiver: exact title
    matches first, from the store catalog and then GOG Database (delisted games); then a single loose match.
    Searched by the installer's name and its product name, never by the folder's name."""
    file_name, file_version = parse_setup(setup)
    names = [n for n in (file_name.replace("_", " "), product_name) if n and normalize_title(n)]
    loose_names = list(names)
    if product_name and name_is_cut(product_name):
        # a cut name is incomplete at the end: its whole " - " parts ("The Elder Scrolls IV - Oblivion") may
        # match loosely. Never done for a full name: "Baldur's Gate - The Original Saga" isn't "Baldur's Gate".
        parts = whole_words(product_name).split(" - ")
        loose_names += [" - ".join(parts[:i]) for i in range(len(parts) - 1, 1, -1)]
    terms = []
    for n in loose_names:
        for t in search_terms(n):
            if t.lower() not in [x.lower() for x in terms]:
                terms.append(t)
    found, failures = None, []
    for relaxed in (False, True):
        for match in (catalog_match, lambda t, n, r: (lambda pid: game_by_id(pid) if pid else None)(gogdb_match(t, n, r))):
            for term in terms:
                try:
                    found = match(term, loose_names if relaxed else names, relaxed)
                except (OSError, ValueError, KeyError) as e:
                    failures.append(e)
                    continue
                if found:
                    break
            if found:
                break
        if found:
            break
    if not found:
        if failures:
            raise failures[-1]  # offline: don't remember "no match"
        return {}
    found["install_size"] = None
    try:
        builds = decode_json(http_get(f"https://content-system.gog.com/products/{found['id']}/os/windows/builds?generation=2"))
        build = match_build(file_version, builds.get("items", [])) or match_build(version, builds.get("items", []))
        if build:
            found["install_size"] = depots_size(decode_json(http_get(build["link"])), found["id"])
    except (OSError, ValueError, KeyError):
        pass
    titles = [found["title"], product_name or clean_title(setup)]
    found["steam_appid"] = steam_match(titles, found["title"]) or 0
    return found


LOOKUP_KEYS = ("install_size", "steam_appid", "cover_vertical", "cover_horizontal", "logo")


def apply_lookup(game, info):
    """GOG's data on a listed game. The installer's own name stays the title unless it was cut short."""
    product = game.get("product_name")
    if not product or name_is_cut(product):
        game["title"] = info.get("title") or (whole_words(product) if product else game["title"])
    game["gog_id"] = info.get("id", "")
    for k in LOOKUP_KEYS:
        game[k] = info.get(k)


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

    def __init__(self, game, out):
        super().__init__(daemon=True)
        self.game, self.out, self.proc, self.stopped = game, out, None, False

    def run(self):
        try:
            setup = os.path.join(self.game["root"], self.game.get("folder", self.game["slug"]), self.game["setup"])
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
    entry = {
        "title": game["title"] or task.get("title"), "exe": task["exe"], "workdir": task["workdir"],
        "args": task.get("args", ""), "installed_size": dir_size(root), "in_steam": False,
        "gog_id": game.get("gog_id") or task.get("gog_id", ""), "icon": icons[0] if icons else "",
        "game_id": game.get("id", ""), "setup": game.get("setup", ""), "version": game.get("version", ""),
    }
    for k in LOOKUP_KEYS[1:]:
        if game.get(k) is not None:
            entry[k] = game[k]
    state["games"][game["slug"]] = entry
    save_state(state)


def forget_removed(state):
    """Forget games whose prefix is gone: removing a non-Steam game from Steam deletes its prefix.
    They show as not installed again. Returns the titles forgotten."""
    gone = [slug for slug, g in state["games"].items()
            if not os.path.isdir(prefix_dir(slug)) and not os.path.exists(g.get("exe", ""))]
    for slug in gone:
        del state["games"][slug]
    if gone:
        save_state(state)
    return gone


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


def steam_art_urls(appid):
    return {k: [f"{STEAM_CDN}/{appid}/{name}"] for k, name in STEAM_ART.items()} if appid else {}


def gog_art_urls(g):
    """GOG's art for a game: portrait box art (342x482), wide key art and logo."""
    out = {}
    if g.get("cover_vertical"):
        out["p"] = [g["cover_vertical"], g["cover_vertical"].replace("_glx_vertical_cover", "")]
    if g.get("cover_horizontal"):
        out[""] = out["_hero"] = [g["cover_horizontal"]]
    if g.get("logo"):
        out["_logo"] = [g["logo"]]
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
        return False
    sources = glob.glob(f"{glob.escape(grid)}/{appid}_hero.*") or glob.glob(f"{glob.escape(grid)}/{appid}.*")
    if not sources:
        return False
    with open(sources[0], "rb") as f:
        image = f.read()
    ext = image_ext(image)
    if not ext:
        return False
    r = subprocess.run(["rsvg-convert", "-o", f"{grid}/{appid}p.png"], capture_output=True,
                       input=cover_svg(title, image, ext).encode())
    log(f"made a cover for {title}: {'ok' if r.returncode == 0 else 'failed'}")
    return r.returncode == 0


def fetch_art_for(g, appid, grids, log=print):
    """Steam's art when the game is on Steam (600x900 cover, header, hero, logo), else GOG's. A cover this
    app made earlier is replaced once real box art turns up; art set by hand is never touched."""
    made = [p for grid in grids for p in glob.glob(f"{glob.escape(grid)}/{appid}p.*")] if g.get("made_cover") else []
    missing = [k for k in STEAM_ART if not all(has_art(grid, appid, k) for grid in grids) or (k == "p" and made)]
    if not missing:
        return
    urls = {}
    for source in (steam_art_urls(g.get("steam_appid")), gog_art_urls(g)):
        for k, u in source.items():
            urls.setdefault(k, []).extend(u)
    saved = []
    for k in missing:
        for url in urls.get(k, []):
            try:
                data = http_get(url)
            except (OSError, ValueError):
                continue
            ext = image_ext(data)
            if not ext:
                continue
            if k == "p" and made:
                for p in made:
                    os.remove(p)
                g["made_cover"] = False
            for grid in grids:
                os.makedirs(grid, exist_ok=True)
                if not has_art(grid, appid, k):
                    with open(f"{grid}/{appid}{k}.{ext}", "wb") as f:
                        f.write(data)
            saved.append(k or "wide")
            break
    log(f"art for {g['title']}: {', '.join(saved) or 'none found'}")


def self_art(grids, log=print):
    """GOG Backup Installer's own cover, drawn from the SVGs that ship with the app."""
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


def gog_data_for_installed(g):
    """Games installed before GOG's art was recorded: fill it in by GOG id, and find the Steam app."""
    if g.get("gog_id") and "cover_vertical" not in g:
        try:
            info = game_by_id(g["gog_id"])
            g.update({k: info[k] for k in ("cover_vertical", "cover_horizontal", "logo")})
            g.setdefault("gog_title", info["title"])
        except (OSError, ValueError, KeyError):
            pass
    if g.get("steam_appid") is None:
        try:
            g["steam_appid"] = steam_match([g["title"]], g.get("gog_title")) or 0
        except (OSError, ValueError):
            pass


def fetch_all_art(state, log=print):
    refresh_from_gog_info(state)
    grids = [f"{STEAM}/userdata/{u}/config/grid" for u in steam_users()]
    try:
        self_art(grids, log)
    except OSError as e:
        log(f"own art failed: {e}")
    for slug, g in state["games"].items():
        appid = shortcut_appid(slug)[0]
        if "cover_vertical" not in g and "made_cover" not in g:
            # installed by an older version, which only saved Steam's .jpg portraits or covers it made as .png
            g["made_cover"] = any(glob.glob(f"{glob.escape(grid)}/{appid}p.png") for grid in grids)
        gog_data_for_installed(g)
        fetch_art_for(g, appid, grids, log)
        for grid in grids:
            try:
                if make_cover(g["title"], appid, grid, log):
                    g["made_cover"] = True
            except OSError as e:
                log(f"cover failed for {g['title']}: {e}")
    save_state(state)


# --- per-game fixes -----------------------------------------------------------------------
# A fix is one launch option in a normal form: `ENV="value" ENV2=value -arg`. Suggestions come from
# ProtonDB's monthly export (ODbL, github.com/bdefore/protondb-data), which is keyed by Steam app id.
# Nothing is applied unless the player turns it on.

SAFE_ENV = re.compile(r"^(PROTON_[A-Z0-9_]+|WINEDLLOVERRIDES|DXVK_[A-Z0-9_]+|VKD3D_[A-Z0-9_]+|WINE_[A-Z0-9_]+|"
                      r"SDL_[A-Z0-9_]+|LC_ALL|RADV_[A-Z0-9_]+|MESA_[A-Z0-9_]+)$")
SAFE_ARG = re.compile(r"^-[A-Za-z][A-Za-z0-9_\-=.]*$")
STANDARD_FIXES = [
    ("Native DirectDraw (old 2D games)", 'WINEDLLOVERRIDES="ddraw=n,b"'),
    ("WineD3D instead of DXVK", "PROTON_USE_WINED3D=1"),
    ("Large address aware", "PROTON_FORCE_LARGE_ADDRESS_AWARE=1"),
    ("No fsync", "PROTON_NO_FSYNC=1"),
]
PROTONDB_INDEX = f"{DATA}/protondb-fixes.json"
PROTONDB_RATINGS = f"{DATA}/protondb-ratings.json"
PROTONDB_EXPORTS = "https://api.github.com/repos/bdefore/protondb-data/contents/reports"
PROTONDB_RAW = "https://raw.githubusercontent.com/bdefore/protondb-data/master/reports/"
LOGS = f"{DATA}/logs"
RECENT = 3 * 365 * 86400


def parse_launch_option(text):
    """(env {name: value}, [args]) when every part is safe to copy, else None."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    env, args, after_command = {}, [], False
    for t in tokens:
        if t == "%command%":
            after_command = True
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", t)
        if m and not after_command:
            if not SAFE_ENV.match(m.group(1)):
                return None
            value = m.group(2)
            if m.group(1) == "WINEDLLOVERRIDES":
                value = re.sub(r"\.dll\b", "", value.lower())
            env[m.group(1)] = value
        elif SAFE_ARG.match(t):
            args.append(t)
        else:
            return None
    return (env, args) if env or args else None


def format_env(name, value):
    return f'{name}="{value}"' if re.search(r"[^A-Za-z0-9_./:-]", value) else f"{name}={value}"


def canonical_launch_option(text):
    """The normal form of a launch option, or None if any part isn't on the safe list."""
    parsed = parse_launch_option(text)
    if not parsed:
        return None
    env, args = parsed
    return " ".join([format_env(k, env[k]) for k in sorted(env)] + args)


def count_fix(counts, report, now):
    """Add one report to {fix: players} if it worked recently with a safe launch option."""
    resp = report.get("responses") or {}
    if resp.get("verdict") != "yes" or not resp.get("launchOptions"):
        return
    if now - int(report.get("timestamp") or 0) > RECENT:
        return
    fix = canonical_launch_option(resp["launchOptions"])
    if fix:
        counts[fix] = counts.get(fix, 0) + 1


def rank_fixes(counts, limit=3, minimum=2):
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(fix, n) for fix, n in ranked if n >= minimum][:limit]


def suggest_fixes(reports, now, limit=3, minimum=2):
    """[(fix, players)]: launch options from recent reports where the game then worked."""
    counts = {}
    for r in reports:
        count_fix(counts, r, now)
    return rank_fixes(counts, limit, minimum)


def compose_launch_options(fixes, gog_args="", log_dir=None):
    """Steam launch options: `ENV=... %command% <fix args> <GOG args>`. Overrides for the same DLLs merge."""
    env, args = {}, []
    for fix in fixes:
        parsed = parse_launch_option(fix)
        if not parsed:
            continue
        for k, v in parsed[0].items():
            env[k] = f"{env[k]};{v}" if k == "WINEDLLOVERRIDES" and k in env and v not in env[k] else v
        args += [a for a in parsed[1] if a not in args]
    if log_dir:
        env["PROTON_LOG"], env["PROTON_LOG_DIR"] = "1", log_dir
    tail = " ".join(args + ([gog_args] if gog_args else []))
    if not env and not args:
        return gog_args
    return " ".join([format_env(k, env[k]) for k in sorted(env)] + ["%command%"] + ([tail] if tail else []))


def report_stream(raw, chunk=1 << 20):
    """Reports from a ProtonDB export, one at a time, without holding the whole file in memory."""
    start = re.compile(r'\{\s*"app":\s*\{')  # the export is pretty-printed
    dec, text = json.JSONDecoder(), ""
    utf8 = codecs.getincrementaldecoder("utf-8")()
    eof = False
    while not eof:
        data = raw.read(chunk)
        eof = not data
        text += utf8.decode(data, final=eof)
        pos = 0
        while True:
            m = start.search(text, pos)
            if not m:
                # keep a short tail: the next chunk may finish a '{"app": {' that starts here
                pos = max(pos, len(text) - 32)
                break
            try:
                obj, end = dec.raw_decode(text, m.start())
            except ValueError:
                if eof:
                    pos = m.end()  # a broken report at the very end: skip it
                    continue
                pos = m.start()  # wait for the rest of this report
                break
            yield obj
            pos = end
        text = text[pos:]


def latest_export(names):
    """The newest reports_<mon><day>_<year>.tar.gz name."""
    months = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
    best = None
    for n in names:
        m = re.match(r"^reports_([a-z]{3})(\d+)_(\d{4})\.tar\.gz$", n)
        if m and m.group(1) in months:
            key = (int(m.group(3)), months[m.group(1)], int(m.group(2)))
            if not best or key > best[0]:
                best = (key, n)
    return best[1] if best else None


def build_protondb_index(raw, now, log=print):
    """{appid: [[fix, players], ...]} for every game that has a suggestion. Counts as it reads."""
    per_app, total = {}, 0
    for r in report_stream(raw):
        total += 1
        aid = str(((r.get("app") or {}).get("steam") or {}).get("appId") or "")
        if aid:
            count_fix(per_app.setdefault(aid, {}), r, now)
    out = {aid: [list(f) for f in rank_fixes(counts)] for aid, counts in per_app.items() if rank_fixes(counts)}
    log(f"protondb: {total} reports, suggestions for {len(out)} games")
    return out


def update_protondb_index(force=False, log=print):
    """Refresh the suggestion index from the newest export, at most every 30 days."""
    try:
        with open(PROTONDB_INDEX) as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    if not force and old and time.time() - old.get("built", 0) < 30 * 86400:
        return
    names = [x["name"] for x in json.loads(http_get(PROTONDB_EXPORTS))]
    export = latest_export(names)
    if not export or (export == old.get("export") and not force):
        return
    req = urllib.request.Request(PROTONDB_RAW + export, headers={"User-Agent": "Mozilla/5.0 GOGInstaller"})
    with urllib.request.urlopen(req, timeout=60) as resp, tarfile.open(fileobj=resp, mode="r|gz") as tar:
        for member in tar:
            f = tar.extractfile(member)
            if f and member.name.endswith(".json"):
                apps = build_protondb_index(f, time.time(), log)
                break
        else:
            raise ValueError("no reports in " + export)
    os.makedirs(DATA, exist_ok=True)
    with open(PROTONDB_INDEX + ".tmp", "w") as f:
        json.dump({"export": export, "built": time.time(), "apps": apps}, f)
    os.replace(PROTONDB_INDEX + ".tmp", PROTONDB_INDEX)


def protondb_suggestions(appid):
    try:
        with open(PROTONDB_INDEX) as f:
            return [tuple(x) for x in json.load(f).get("apps", {}).get(str(appid), [])]
    except (OSError, ValueError):
        return []


def protondb_rating(appid):
    """{"tier", "total"} from ProtonDB's summary, cached for a week; None if there's none."""
    try:
        with open(PROTONDB_RATINGS) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(str(appid))
    if hit and time.time() - hit.get("at", 0) < 7 * 86400:
        return hit if hit.get("tier") else None
    try:
        data = json.loads(http_get(f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json"))
        hit = {"tier": data.get("tier"), "total": data.get("total"), "at": time.time()}
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        hit = {"tier": None, "total": 0, "at": time.time()}
    cache[str(appid)] = hit
    os.makedirs(DATA, exist_ok=True)
    with open(PROTONDB_RATINGS, "w") as f:
        json.dump(cache, f)
    return hit if hit.get("tier") else None


def start_protondb_update():
    """Refresh suggestions in the background at low priority; it's quick to skip when fresh."""
    subprocess.Popen(["nice", "-n", "19", sys.executable, os.path.abspath(__file__), "--update-protondb"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def valve_tool_name(folder):
    """Steam's internal name for one of Valve's Proton folders, or None."""
    if folder == "Proton - Experimental":
        return "proton_experimental"
    if folder == "Proton Hotfix":
        return "proton_hotfix"
    m = re.match(r"^Proton (\d+)\.(\d+)(?: \(Beta\))?$", folder)
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2))
    return f"proton_{major}" if major >= 7 or minor == 0 else f"proton_{major}{minor}"


def custom_tool_names(vdf_text):
    """Tool names in a compatibilitytool.vdf that run Windows games."""
    out = []
    vdf_text = re.sub(r"(^|\s)//[^\n]*", r"\1", vdf_text)  # strip // comments
    m = re.search(r'"compat_tools"\s*\{(.*)\}', vdf_text, re.S)
    if not m:
        return out
    for name, body in re.findall(r'"([^"]+)"\s*\{([^{}]*)\}', m.group(1)):
        if re.search(r'"from_oslist"\s*"windows"', body) and re.search(r'"to_oslist"\s*"linux"', body):
            out.append(name)
    return out


def known_tool_names():
    """Proton names Steam itself knows, read from its app info cache; used to verify guessed names."""
    try:
        with open(f"{STEAM}/appcache/appinfo.vdf", "rb") as f:
            return set(re.findall(rb"proton_[a-z0-9]+", f.read()))
    except OSError:
        return set()


def installed_proton_tools():
    """[(label, name)] for Proton versions on this Deck that Steam can use, Experimental first."""
    known = known_tool_names()
    tools = []
    for folder in sorted(os.listdir(COMMON) if os.path.isdir(COMMON) else []):
        name = valve_tool_name(folder)
        if name and os.path.exists(f"{COMMON}/{folder}/proton") and (name.encode() in known or name == COMPAT_TOOL[1]):
            tools.append((folder, name))
    custom = f"{STEAM}/compatibilitytools.d"
    for folder in sorted(os.listdir(custom) if os.path.isdir(custom) else []):
        try:
            with open(f"{custom}/{folder}/compatibilitytool.vdf", errors="replace") as f:
                tools += [(n, n) for n in custom_tool_names(f.read())]
        except OSError:
            pass
    tools.sort(key=lambda t: (t[1] != COMPAT_TOOL[1], t[0]))
    return tools


# --- GOG's DDrawCompat ------------------------------------------------------------------
# GOG patches some old games to load DDrawCompat (a DirectDraw wrapper for modern Windows) under
# another name, e.g. Heroes3.exe imports DirectDrawCreate from xdd.dll. It crashes under Wine. Renaming
# that import to "ddraw" makes the game use Wine's own DirectDraw instead. The original exe is kept.

DDRAW_EXPORTS = {"DirectDrawCreate", "DirectDrawCreateEx", "DirectDrawCreateClipper", "DirectDrawEnumerateA",
                 "DirectDrawEnumerateW", "DirectDrawEnumerateExA", "DirectDrawEnumerateExW"}
ORIGINAL_SUFFIX = ".gog"


def pe_imports(data):
    """[(dll name, file offset of the name, [function names])] of a 32- or 64-bit PE file."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    nsec, osz = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    pe64 = magic == 0x20B
    imp_rva = struct.unpack_from("<I", data, opt + (120 if pe64 else 104))[0]
    secs = [struct.unpack_from("<8sIIII", data, opt + osz + i * 40) for i in range(nsec)]

    def off(rva):
        for _, vsize, va, rsize, raw in secs:
            if va <= rva < va + max(vsize, rsize):
                return rva - va + raw
        raise ValueError("address outside the file")

    def cstr(o):
        return data[o:data.index(b"\0", o)].decode("latin-1")
    out = []
    if not imp_rva:
        return out
    i = off(imp_rva)
    while True:
        oft, _, _, name, ft = struct.unpack_from("<5I", data, i)
        i += 20
        if not name:
            break
        fns, t = [], off(oft or ft)
        size, flag = (8, 1 << 63) if pe64 else (4, 1 << 31)
        while True:
            v = struct.unpack_from("<Q" if pe64 else "<I", data, t)[0]
            t += size
            if not v:
                break
            fns.append(f"#{v & 0xFFFF}" if v & flag else cstr(off(v & 0x7FFFFFFF) + 2))
        out.append((cstr(off(name)), off(name), fns))
    return out


def ddrawcompat_import(exe):
    """(name, offset) of an import that loads GOG's DDrawCompat in place of ddraw.dll, or None."""
    with open(exe, "rb") as f:
        data = f.read()
    folder = os.path.dirname(exe)
    for name, offset, fns in pe_imports(data):
        if name.lower() == "ddraw.dll" or len(name) < 6 or not fns or not set(fns) <= DDRAW_EXPORTS:
            continue
        dll = os.path.join(folder, name)
        if not os.path.isfile(dll):
            dll = next((os.path.join(folder, f) for f in os.listdir(folder) if f.lower() == name.lower()), None)
        if dll:
            with open(dll, "rb") as f:
                if b"DDrawCompat" in f.read():
                    return name, offset
    return None


def ddrawcompat_state(exe):
    """"on" when the game was switched to Wine's DirectDraw, "off" when it still loads DDrawCompat,
    None when it doesn't use DDrawCompat at all."""
    try:
        if os.path.isfile(exe + ORIGINAL_SUFFIX) and ddrawcompat_import(exe + ORIGINAL_SUFFIX):
            return "on" if not ddrawcompat_import(exe) else "off"
        return "off" if ddrawcompat_import(exe) else None
    except (OSError, ValueError, struct.error):
        return None


def set_ddrawcompat_bypass(exe, on):
    """Switch a game between GOG's DDrawCompat and Wine's DirectDraw. The original exe is kept next to it."""
    original = exe + ORIGINAL_SUFFIX
    if not on:
        if os.path.isfile(original):
            os.replace(original, exe)
        return
    found = ddrawcompat_import(exe)
    if not found:
        return
    name, offset = found
    if not os.path.exists(original):
        shutil.copy2(exe, original)
    with open(exe, "r+b") as f:
        f.seek(offset)
        f.write(b"ddraw".ljust(len(name), b"\0"))


def log_dir(slug):
    return f"{LOGS}/{shortcut_appid(slug)[0]}"


def proton_log_tail(slug, limit=6):
    """The last distinct error lines of the newest Proton log for a game."""
    logs = sorted(glob.glob(f"{glob.escape(log_dir(slug))}/*.log"), key=os.path.getmtime)
    if not logs:
        return None
    with open(logs[-1], errors="replace") as f:
        lines = f.read().splitlines()
    picked = []
    for line in lines:
        if re.search(r"\berr:|Unhandled exception|wine: |fixme:dll|Assertion|could not load", line):
            short = re.sub(r"^[0-9a-f]+:\s*", "", line.strip())[:160]
            if short not in picked:
                picked.append(short)
    return {"file": logs[-1], "when": os.path.getmtime(logs[-1]), "lines": picked[-limit:]}


def game_launch_options(slug, g):
    return compose_launch_options(g.get("fixes", []), g.get("args", ""), log_dir(slug) if g.get("log") else None)


def steam_users():
    base = f"{STEAM}/userdata"
    return [d for d in os.listdir(base) if d.isdigit() and d != "0"] if os.path.isdir(base) else []


def pending_games(state):
    """Games to add to Steam, or whose Proton or fixes changed since they were added."""
    return {s: g for s, g in state["games"].items() if not g.get("in_steam") or g.get("needs_update")}


def write_steam_files(state, include_self=True):
    """Write shortcuts and Proton settings. Steam must be stopped. Returns the number of games added."""
    pending = pending_games(state)
    entries = []
    for slug, g in pending.items():
        entries.append(shortcut_entry(shortcut_appid(slug)[1], g["title"], g["exe"], g["workdir"],
                                      game_launch_options(slug, g), g.get("icon", "")))
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
        for slug, g in pending.items():
            text = set_compat_tool(text, shortcut_appid(slug)[0], g.get("proton") or COMPAT_TOOL[1])
        with open(cfg + ".tmp", "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
        os.replace(cfg + ".tmp", cfg)
    for slug, g in pending.items():
        g["in_steam"], g["needs_update"] = True, False
        if g.get("log"):
            os.makedirs(log_dir(slug), exist_ok=True)
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
        forget_removed(self.state)
        self.games, self.selected, self.top = [], 0, 0
        self.screen, self.job, self.current = "connecting", None, None
        self.events = queue.Queue()
        self.stop_armed_until, self.message = 0, ""
        self.exes, self.exe_sel, self.exe_top = [], 0, 0
        self.marked, self.queue, self.queue_total, self.queue_done, self.failed = set(), [], 0, 0, []
        self.draft, self.set_sel, self.pick, self.pick_values, self.pick_sel, self.pick_top = None, 0, [], [], 0, 0
        self.field, self.browse_path, self.busy, self.edit_idx, self.edit_sel = "", "", "", 0, 0
        self.source_errors, self.connected = [], []
        self.game_slug, self.game_sel, self.game_top, self.game_info, self.tools = None, 0, 0, {}, None
        self.pad = Gamepad()
        self.bind_keys()
        self.canvas.bind("<Button-1>", self.on_click)
        self.draw()
        start_protondb_update()
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
        sources = list(self.cfg["sources"])

        def work():
            found, errors, connected = [], [], []
            for src in sources:
                root, err = connect_source(src)
                if err:
                    errors.append((src, err))
                    continue
                connected.append(src)
                try:
                    games = scan_folder(root)
                except OSError as e:
                    errors.append((src, str(e)))
                    continue
                for g in games:
                    g["root"] = root
                found.append((src, games))
            self.events.put(("games", merge_games(found, dict(self.state["games"])), errors, connected))
        threading.Thread(target=work, daemon=True).start()

    def disconnect_all(self):
        for src in self.connected:
            disconnect_source(src)
        self.connected = []

    def lookup_sizes(self, games):
        """Fill in GOG's titles and sizes behind the list; a network failure just leaves sizes blank."""
        cache = load_json(LOOKUP_FILE)
        for g in games:
            if g["setup"] in cache:
                self.events.put(("lookup", g["slug"], cache[g["setup"]]))
                continue
            try:
                info = gog_lookup(g["setup"], g.get("product_name"), g.get("version"))
            except (OSError, ValueError, KeyError):
                continue  # not cached, so it's tried again next time
            cache[g["setup"]] = info
            save_json(LOOKUP_FILE, cache)
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
        if self.source_errors:
            k = len(self.source_errors)
            self.text(200, 128, f"{k} source{'s' if k > 1 else ''} not connected", 18, WARN, True)
        if n > VISIBLE_ROWS:
            self.text(1240, 128, f"{self.selected + 1} of {n}", 18, MUTED, True, "e")
        if not n:
            self.text(40, 210, "No installers found", 42, TEXT, True)
            self.text(40, 262, "Put each game in its own folder, with its setup_*.exe directly inside it.", 28, MUTED)
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
            label = {"install": "Install", "nospace": "Not enough space"}.get(kind, "Options" if n else "Install")
            can_a = kind in ("install", "added", "installed")
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
        updates = sum(1 for g in pending if g.get("in_steam"))
        verb = "Update" if updates == n else ("Add" if not updates else "Add or update")
        self.text(40, 100, f"{verb} {n} game{'s' if n != 1 else ''} in Steam?", 72, TEXT, True)
        y = 164
        self.rect(40, y, 1240, y + 2, LINE)
        for g in pending[:3]:
            self.text(40, y + 44, self.fit(g["title"], 34, True, 1000), 34, TEXT, True)
            note = "new settings" if g.get("in_steam") else fmt_size(g.get("installed_size", 0))
            self.text(1240, y + 44, note, 26, MUTED, False, "e")
            y += 84
            self.rect(40, y, 1240, y + 2, LINE)
        if n > 3:
            self.text(40, y + 30, f"and {n - 3} more", 26, MUTED)
            y += 50
        self.text(40, y + 46, "Steam closes and reopens, which takes about 20 seconds. This app closes too.", 32)
        self.text(40, y + 90, "The games will be in your library when Steam is back.", 32)
        self.buttons(("Add and restart Steam", self.action_a), ("Not now", self.action_b))

    def game_rows(self):
        """[(kind, label, value, on)] for the Game screen; kind: proton, fix, log, viewlog."""
        g = self.state["games"][self.game_slug]
        info = self.game_info
        tools = self.tools or [(COMPAT_TOOL[0], COMPAT_TOOL[1])]
        tool = g.get("proton") or COMPAT_TOOL[1]
        label = next((t[0] for t in tools if t[1] == tool), tool)
        rows = [("proton", "Proton", label, None)]
        if info.get("ddrawcompat"):
            rows.append(("patch", "Skip GOG's DDrawCompat", "it crashes under Wine · edits the exe",
                         info["ddrawcompat"] == "on"))
        offered = []
        for fix, players in info.get("suggestions", []):
            rows.append(("fix", fix, f"{players} players on ProtonDB", fix in g.get("fixes", [])))
            offered.append(fix)
        for name, fix in STANDARD_FIXES:
            if fix not in offered:
                rows.append(("fix", name, fix, fix in g.get("fixes", [])))
        for fix in g.get("fixes", []):  # chosen earlier, no longer offered: keep it visible so it can be turned off
            if fix not in offered and fix not in [f for _, f in STANDARD_FIXES]:
                rows.append(("fix", fix, "", True))
        rows.append(("log", "Save a log next launch", "PROTON_LOG=1", bool(g.get("log"))))
        if info.get("log"):
            rows.append(("viewlog", "Last log", time.strftime("%d %b %H:%M", time.localtime(info["log"]["when"])), None))
        return rows

    def draw_game(self):
        g = self.state["games"][self.game_slug]
        info = self.game_info
        self.header(False)
        self.text(40, 160, self.fit(g["title"], 56, True, 1200), 56, TEXT, True)
        if info.get("busy"):
            line = "Looking up ProtonDB…"
        elif info.get("rating"):
            r = info["rating"]
            line = f"ProtonDB: {r['tier'].capitalize()} · {r['total']} reports"
        elif info.get("steam_appid"):
            line = "No ProtonDB rating"
        else:
            line = "Not on Steam, so ProtonDB has no reports for it"
        self.text(40, 212, line, 26, MUTED)
        rows = self.game_rows()
        visible = 6
        self.game_sel = min(self.game_sel, len(rows) - 1)
        if self.game_sel < self.game_top:
            self.game_top = self.game_sel
        elif self.game_sel >= self.game_top + visible:
            self.game_top = self.game_sel - visible + 1
        y = 248
        for i in range(self.game_top, min(self.game_top + visible, len(rows))):
            kind, label, value, on = rows[i]
            focus = i == self.game_sel
            if focus:
                self.rect(0, y, 1280, y + 70, FOCUS)
            else:
                self.rect(0, y + 68, 1280, y + 70, ROW_LINE)
            fg = GROUND if focus else TEXT
            cy = y + 35
            x = 40
            if kind in ("fix", "log", "patch"):
                self.checkbox(40, cy, on, fg, GROUND)
                x = 84
            self.text(x, cy, self.fit(label, 28, True, 620), 28, fg, True)
            self.text(1240, cy, self.fit(value, 24, False, 480), 24, GROUND if focus else MUTED, False, "e")
            self.hits.append((0, y, 1280, y + 70, lambda i=i: self.tap_game_row(i)))
            y += 70
        if len(rows) > visible:
            self.text(1240, 212, f"{self.game_sel + 1} of {len(rows)}", 20, MUTED, True, "e")
        self.rect(0, 724, 1280, 800, BAND)
        kind = rows[self.game_sel][0]
        action = {"proton": "Change", "viewlog": "View"}.get(kind, "Turn off" if rows[self.game_sel][3] else "Turn on")
        count = self.pending_count()
        self.legend([("A", action, True, self.action_a),
                     ("Y", f"Add to Steam ({count})", count > 0, self.action_y),
                     ("B", "Back", True, self.action_b)])

    def draw_game_log(self):
        info = self.game_info.get("log") or {"lines": [], "file": ""}
        self.header(False)
        self.text(40, 160, "Last Proton log", 56, TEXT, True)
        self.text(40, 212, self.fit(info["file"].replace(HOME, "~"), 24, False, 1200), 24, MUTED)
        y = 260
        for line in info["lines"] or ["No errors found in the log."]:
            self.text(40, y, self.fit(line, 24, False, 1200), 24, TEXT)
            y += 52
        self.buttons(("Back", self.action_b))

    def draw_offline(self):
        self.header(False)
        if not self.cfg["sources"]:
            self.text(40, 200, "Add where your installers are", 72, TEXT, True)
            self.text(40, 280, "Press Start for Settings, then add an SMB share or a USB drive.", 32)
            self.buttons(("Open Settings", self.action_menu), ("Quit", self.action_b))
            return
        self.text(1240, 54, "Not connected", 22, WARN, False, "e")
        self.text(40, 200, "Can’t reach your installers", 72, TEXT, True)
        self.text(40, 280, "Check that the server is on and on the same network, or that the drive is plugged in.", 32)
        y = 350
        for src, err in self.source_errors[:4]:
            kind, where, folder = source_label(src)
            self.text(40, y, self.fit(f"{kind}  {where}  {folder}", 26, True, 560), 26, MUTED, True)
            self.text(640, y, self.fit(err, 24, False, 600), 24, MUTED)
            y += 44
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

    def edit_rows(self):
        """(label, value, editable) for the source being edited."""
        src = self.draft[self.edit_idx]
        if src["type"] == "smb":
            return [("Server", src.get("server") or "Choose…", True), ("Share", src.get("share") or "Choose…", True),
                    ("Folder", src.get("folder") or "(top of the share)", True)]
        return [("Drive", src.get("label") or "Choose…", True),
                ("Folder", src.get("folder") or "(top of the drive)", True)]

    def row_list(self, rows, sel, top=256, height=84):
        """Settings-style rows: [(left, right, enabled)]; returns the y after the last row."""
        y = top
        for i, (left, right, enabled) in enumerate(rows):
            on = i == sel
            if on:
                self.rect(0, y, 1280, y + height, FOCUS)
            else:
                self.rect(0, y + height - 2, 1280, y + height, ROW_LINE)
            fg = GROUND if on else (TEXT if enabled else MUTED)
            self.text(40, y + height / 2, left, 30, fg, True)
            self.text(1240, y + height / 2, self.fit(right, 28, False, 980), 28, fg, False, "e")
            self.hits.append((0, y, 1280, y + height, lambda i=i: self.tap_row_in_list(i)))
            y += height
        return y

    def draw_settings(self):
        self.header(False)
        self.text(40, 160, "Settings", 56, TEXT, True)
        self.text(40, 216, "Where your GOG installers are. Games from every source show in one list.", 28, MUTED)
        rows = []
        for src in self.draft:
            kind, where, folder = source_label(src)
            rows.append((kind, f"{where}   {folder}", True))
        rows.append(("Add a source", "", True))
        first = max(0, self.set_sel - 4)
        self.row_list(rows[first:first + 5], self.set_sel - first)
        on_source = self.set_sel < len(self.draft)
        self.buttons(("Edit" if on_source else "Add", self.action_a), ("Done", self.action_b))
        self.legend([("X", "Remove", on_source, self.action_x)], 740)

    def draw_edit_source(self):
        self.header(False)
        kind = source_label(self.draft[self.edit_idx])[0]
        self.text(40, 160, {"SMB": "SMB share", "USB": "USB drive"}[kind], 56, TEXT, True)
        self.row_list(self.edit_rows(), self.edit_sel)
        self.buttons(("Change", self.action_a), ("Back", self.action_b))
        self.legend([("X", "Remove source", True, self.action_x)], 740)

    def draw_edit_field(self):
        self.header(False)
        self.text(40, 160, "Server address", 56, TEXT, True)
        self.text(40, 216, "Press Steam + X for the on-screen keyboard", 28, MUTED)
        self.rect(40, 270, 1240, 360, FOCUS)
        self.text(64, 315, self.field + "_", 40, GROUND, True)
        self.buttons(("Done", self.action_a), ("Cancel", self.action_b))

    PICK_TITLES = {"pick_type": "Add a source", "pick_share": "Choose a share", "pick_drive": "Choose a drive",
                   "pick_folder": "Choose a folder"}
    PICK_EMPTY = {"pick_share": "No shares found for guests",
                  "pick_drive": "No drive found. Plug one in, then press A to look again.",
                  "pick_folder": "No folders here. Press B to use this one."}

    def draw_pick(self):
        self.header(False)
        self.text(40, 160, self.PICK_TITLES[self.screen], 56, TEXT, True)
        if self.screen == "pick_folder":
            src = self.draft[self.edit_idx]
            base = src.get("share") if src["type"] == "smb" else source_label(src)[1]
            self.text(40, 216, f"{base}/{self.browse_path}".rstrip("/"), 28, MUTED)
        if self.busy:
            self.text(40, 300, self.busy, 32, MUTED)
        elif not self.pick:
            self.text(40, 300, self.PICK_EMPTY.get(self.screen, ""), 32, MUTED)
        y = 256
        for i in range(self.pick_top, min(self.pick_top + 5, len(self.pick))):
            on = i == self.pick_sel
            if on:
                self.rect(0, y, 1280, y + 80, FOCUS)
            self.text(40, y + 40, self.fit(self.pick[i], 30, on, 1200), 30, GROUND if on else TEXT, on)
            self.hits.append((0, y, 1280, y + 80, lambda i=i: self.tap_pick(i)))
            y += 80
        if self.screen == "pick_folder":
            self.buttons(("Open", self.action_a), ("Use this folder", self.action_b))
        else:
            self.buttons(("Look again" if self.screen == "pick_drive" and not self.pick and not self.busy
                          else "Choose", self.action_a), ("Back", self.action_b))

    def draw_pick_type(self):
        self.draw_pick()

    def draw_pick_share(self):
        self.draw_pick()

    def draw_pick_drive(self):
        self.draw_pick()

    def draw_pick_folder(self):
        self.draw_pick()

    # game screen actions -------------------------------------------------------------
    def open_game(self, slug):
        self.game_slug, self.game_sel, self.game_top = slug, 0, 0
        self.game_info = {"busy": True}
        g = dict(self.state["games"][slug])

        def work():
            info = {"log": proton_log_tail(slug), "ddrawcompat": ddrawcompat_state(g["exe"])}
            if self.tools is None:
                self.tools = installed_proton_tools()
            try:
                appid = g.get("steam_appid")
                if appid is None:
                    appid = steam_match([g["title"]], g.get("gog_title")) or 0
                info["steam_appid"] = appid
                if appid:
                    info["rating"] = protondb_rating(appid)
                    info["suggestions"] = protondb_suggestions(appid)
            except (OSError, ValueError, KeyError):
                pass  # offline: the standard fixes still work
            self.events.put(("gameinfo", slug, info))
        threading.Thread(target=work, daemon=True).start()
        self.go("game")

    def change_game(self, mutate):
        g = self.state["games"][self.game_slug]
        mutate(g)
        if g.get("in_steam"):
            g["needs_update"] = True
        save_state(self.state)
        self.draw()

    def tap_game_row(self, i):
        if i == self.game_sel:
            self.action_a()
        else:
            self.game_sel = i
            self.draw()

    def game_change(self):
        kind, label, value, on = self.game_rows()[self.game_sel]
        if kind == "proton":
            tools = self.tools or [(COMPAT_TOOL[0], COMPAT_TOOL[1])]
            names = [t[1] for t in tools]

            def next_tool(g):
                current = g.get("proton") or COMPAT_TOOL[1]
                g["proton"] = names[(names.index(current) + 1) % len(names)] if current in names else names[0]
            self.change_game(next_tool)
        elif kind == "fix":
            fix = value if value and parse_launch_option(value) else label

            def toggle(g):
                fixes = list(g.get("fixes", []))
                g["fixes"] = [f for f in fixes if f != fix] if fix in fixes else fixes + [fix]
            self.change_game(toggle)
        elif kind == "log":
            self.change_game(lambda g: g.update(log=not g.get("log")))
        elif kind == "patch":  # a file change: works at the next launch, Steam needs no update
            exe = self.state["games"][self.game_slug]["exe"]
            try:
                set_ddrawcompat_bypass(exe, not on)
            except OSError:
                pass
            self.game_info["ddrawcompat"] = ddrawcompat_state(exe)
            self.draw()
        elif kind == "viewlog":
            self.go("game_log")

    # settings actions ----------------------------------------------------------------
    def action_menu(self):
        if self.screen in ("library", "offline"):
            self.draft, self.set_sel, self.busy = [dict(s) for s in self.cfg["sources"]], 0, ""
            self.go("settings")

    def finish_settings(self):
        """Leaving Settings keeps what's there; sources left unfinished are dropped."""
        complete = [x for x in self.draft if is_complete(x)]
        if complete == self.cfg["sources"]:
            self.go("library" if self.connected else "offline")
            return
        self.disconnect_all()
        self.cfg = {"sources": complete}
        save_config(self.cfg)
        self.games, self.marked = [], set()
        self.go("connecting")
        self.connect()

    def tap_row_in_list(self, i):
        if self.screen == "settings":
            self.set_sel = max(0, self.set_sel - 4) + i
        else:
            self.edit_sel = i
        self.action_a()

    def tap_pick(self, i):
        if i == self.pick_sel:
            self.action_a()
        else:
            self.pick_sel = i
            self.draw()

    def load_pick(self, screen, work):
        """Fetch a list off the UI thread. work() returns [(label, value)]."""
        self.pick, self.pick_values, self.pick_sel, self.pick_top, self.busy = [], [], 0, 0, "Looking…"
        self.go(screen)

        def run():
            try:
                self.events.put(("pick", screen, work()))
            except (OSError, subprocess.SubprocessError, ValueError):
                self.events.put(("pick", screen, []))
        threading.Thread(target=run, daemon=True).start()

    def open_folder(self, path):
        src = dict(self.draft[self.edit_idx])
        self.browse_path = path

        def folders():
            if src["type"] == "smb":
                names = smb_folders(src["server"], src["share"], path)
            else:
                root, err = mount_usb(dict(src, folder=""))
                if err:
                    return []
                base = os.path.join(root, path)
                names = sorted((n for n in os.listdir(base)
                                if not n.startswith(".") and os.path.isdir(os.path.join(base, n))), key=str.lower)
            up = [("..   (up a level)", None)] if path else []
            return up + [(n, n) for n in names]
        self.load_pick("pick_folder", folders)

    def edit_change(self):
        src = self.draft[self.edit_idx]
        label = self.edit_rows()[self.edit_sel][0]
        if label == "Server":
            self.field = src.get("server", "")
            self.go("edit_field")
        elif label == "Share":
            if not src.get("server"):
                return
            server = src["server"]
            self.load_pick("pick_share", lambda: [(x, x) for x in smb_shares(server)])
        elif label == "Drive":
            self.load_pick("pick_drive", lambda: [(f"{drive_name(d)}   {d['fstype']}   {fmt_size(d['size'])}", d)
                                                  for d in list_drives()])
        elif label == "Folder":
            if (src["type"] == "smb" and src.get("share")) or (src["type"] == "usb" and src.get("uuid")):
                self.open_folder(src.get("folder", "").strip("/"))

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
        if self.screen == "game":
            self.game_sel = max(0, min(len(self.game_rows()) - 1, self.game_sel + step))
            self.draw()
        elif self.screen == "settings":
            self.set_sel = max(0, min(len(self.draft), self.set_sel + step))
            self.draw()
        elif self.screen == "edit_source":
            self.edit_sel = max(0, min(len(self.edit_rows()) - 1, self.edit_sel + step))
            self.draw()
        elif self.screen in ("pick_type", "pick_share", "pick_drive", "pick_folder") and self.pick:
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
            self.job = InstallJob(self.current, self.events)
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
        if s == "game":
            self.game_change()
        elif s == "game_log":
            self.go("game")
        elif s == "settings":
            if self.set_sel < len(self.draft):
                self.edit_idx, self.edit_sel = self.set_sel, 0
                self.go("edit_source")
            else:
                self.pick, self.pick_values = ["SMB share", "USB drive or SD card"], ["smb", "usb"]
                self.pick_sel, self.pick_top, self.busy = 0, 0, ""
                self.go("pick_type")
        elif s == "edit_source":
            self.edit_change()
        elif s == "pick_type":
            kind = self.pick_values[self.pick_sel]
            if kind == "smb":
                known = next((x.get("server") for x in self.draft if x.get("type") == "smb" and x.get("server")), "")
                self.draft.append({"type": "smb", "server": known, "share": "", "folder": ""})
            else:
                self.draft.append({"type": "usb", "uuid": "", "label": "", "folder": ""})
            self.edit_idx, self.edit_sel, self.set_sel = len(self.draft) - 1, 0, len(self.draft) - 1
            self.go("edit_source")
        elif s == "edit_field":
            src = self.draft[self.edit_idx]
            if self.field.strip() != src.get("server"):
                src.update(server=self.field.strip(), share="", folder="")
            self.go("edit_source")
        elif s == "pick_share":
            if self.pick:
                self.draft[self.edit_idx].update(share=self.pick_values[self.pick_sel], folder="")
                self.open_folder("")
        elif s == "pick_drive":
            if self.pick:
                d = self.pick_values[self.pick_sel]
                self.draft[self.edit_idx].update(uuid=d["uuid"], label=drive_name(d), folder="")
                self.open_folder("")
            elif not self.busy:
                self.edit_change()
        elif s == "pick_folder":
            if self.pick:
                name = self.pick_values[self.pick_sel]
                self.open_folder(os.path.dirname(self.browse_path) if name is None
                                 else f"{self.browse_path}/{name}".strip("/"))
        elif s == "library":
            if self.marked:
                self.start_queue([g for g in self.games if g["slug"] in self.marked])
            elif self.games and self.row_kind(self.games[self.selected]) == "install":
                self.start_queue([self.games[self.selected]])
            elif self.games and self.row_kind(self.games[self.selected]) in ("added", "installed"):
                self.open_game(self.games[self.selected]["slug"])
        elif s == "add_to_steam":
            start_apply_steam()
            self.quit()
        elif s == "offline":
            if not self.cfg["sources"]:
                self.action_menu()
            else:
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
        if s == "game":
            self.go("library")
        elif s == "game_log":
            self.go("game")
        elif s == "settings":
            self.finish_settings()
        elif s in ("edit_source", "pick_type"):
            self.go("settings")
        elif s in ("edit_field", "pick_share", "pick_drive"):
            self.go("edit_source")
        elif s == "pick_folder":
            if not self.busy:  # B accepts the folder being shown
                self.draft[self.edit_idx]["folder"] = self.browse_path
                self.go("edit_source")
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
        elif self.screen == "settings" and self.set_sel < len(self.draft):
            del self.draft[self.set_sel]
            self.set_sel = min(self.set_sel, len(self.draft))
            self.draw()
        elif self.screen == "edit_source":
            del self.draft[self.edit_idx]
            self.set_sel = min(self.edit_idx, len(self.draft))
            self.go("settings")

    def action_y(self):
        if self.screen in ("library", "game") and self.pending_count():
            self.go("add_to_steam")

    def quit(self):
        if self.job and self.job.is_alive():
            self.job.stop()
            self.job.join(8)
        self.disconnect_all()
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
            self.games, self.source_errors, self.connected = ev[1], ev[2], ev[3]
            self.selected = self.top = 0
            self.go("library" if self.connected else "offline")
            threading.Thread(target=self.lookup_sizes, args=(self.games,), daemon=True).start()
        elif kind == "gameinfo":
            if ev[1] == self.game_slug:
                self.game_info = ev[2]
                g = self.state["games"].get(ev[1])
                if g is not None and g.get("steam_appid") is None and "steam_appid" in ev[2]:
                    g["steam_appid"] = ev[2]["steam_appid"]
                    save_state(self.state)
                if self.screen in ("game", "game_log"):
                    self.draw()
        elif kind == "pick":
            if self.screen == ev[1]:
                self.pick, self.pick_values, self.busy = [p[0] for p in ev[2]], [p[1] for p in ev[2]], ""
                self.draw()
        elif kind == "lookup":
            g = next((x for x in self.games if x["slug"] == ev[1]), None)
            if g:
                apply_lookup(g, ev[2])
                if self.screen == "library":
                    self.draw()
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
    elif "--smbfs" in sys.argv:
        url, mountpoint = sys.argv[sys.argv.index("--smbfs") + 1:][:2]
        sys.exit(SmbFs(url).run(mountpoint, urllib.parse.urlsplit(url).netloc))
    elif "--update-protondb" in sys.argv:
        update_protondb_index(force="--force" in sys.argv)
    elif "--fetch-art" in sys.argv:
        fetch_all_art(load_state())
    elif "--list" in sys.argv:
        for src in load_config()["sources"]:
            kind, where, folder = source_label(src)
            root, err = connect_source(src)
            print(f"== {kind} {where} {folder}: {err or 'connected'}")
            if err:
                continue
            try:
                for g in scan_folder(root):
                    apply_lookup(g, gog_lookup(g["setup"], g.get("product_name"), g.get("version")))
                    size = fmt_size(g["install_size"]) if g.get("install_size") else "size unknown"
                    print(f"{g['title']} | installer {fmt_size(g['size'])} | install {size} | "
                          f"gog {g.get('gog_id') or 'no match'} | {g['setup']}")
            finally:
                disconnect_source(src)
    else:
        App().run()


if __name__ == "__main__":
    main()
