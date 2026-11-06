"""Tests for the parts that touch Steam's files or decide what gets installed."""
import io
import json
import os
import tempfile
import unittest

import gog_installer as g

CONFIG = '''"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"CompatToolMapping"
\t\t\t\t{
\t\t\t\t\t"2100"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"proton_experimental"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"250"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t\t"Other"\t\t"keep {braces} in strings"
\t\t\t}
\t\t}
\t}
}
'''


def existing_shortcuts():
    entry = g.shortcut_entry(-1293752665, "Google Chrome", "/usr/bin/flatpak", "/usr/bin/", "run x")
    entry["LastPlayTime"] = 1234
    entry["custom"] = "kept"
    return g.vdf_dump({"shortcuts": {"0": entry}}) + b"\x08"


class Titles(unittest.TestCase):
    def test_real_installer_name(self):
        self.assertEqual(g.clean_title("setup_hollow_knight_1.5.12620_(64bit)_(89718).exe"), "Hollow Knight")

    def test_small_words(self):
        self.assertEqual(g.clean_title("setup_the_lord_of_the_rings_2.0_(1).exe"), "The Lord of the Rings")

    def test_keeps_digits_in_name(self):
        self.assertEqual(g.clean_title("setup_baldurs_gate_2_v2.6.exe"), "Baldurs Gate 2")
        self.assertEqual(g.clean_title("setup_some_game_12345.exe"), "Some Game")


class Scan(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def add(self, folder, name, size=10):
        os.makedirs(f"{self.d}/{folder}", exist_ok=True)
        with open(f"{self.d}/{folder}/{name}", "wb") as f:
            f.truncate(size)

    def test_real_layout_ignores_extras(self):
        self.add("Hollow Knight & Soundtracks v1.5.12459 GoG", "setup_hollow_knight_1.5.12620_(64bit)_(89718).exe", 100)
        self.add("Hollow Knight & Soundtracks v1.5.12459 GoG/Extras", "setup_soundtrack.exe", 999)
        games = g.scan_folder(self.d)
        self.assertEqual([x["title"] for x in games], ["Hollow Knight"])
        self.assertEqual(games[0]["size"], 100)

    def test_bin_parts_count_and_other_files_dont(self):
        self.add("Game", "setup_game_1.0.exe", 100)
        self.add("Game", "setup_game_1.0-1.bin", 50)
        self.add("Game", "setup_game_1.0-2.bin", 50)
        self.add("Game", "readme.bin", 999)
        self.assertEqual(g.scan_folder(self.d)[0]["size"], 200)

    def test_base_game_chosen_over_dlc(self):
        self.add("Game", "setup_game_1.0.exe")
        self.add("Game", "setup_game_some_dlc_1.0.exe")
        self.assertEqual(g.scan_folder(self.d)[0]["setup"], "setup_game_1.0.exe")

    def test_folder_without_installer_skipped(self):
        self.add("Empty", "notes.txt")
        self.add(".hidden", "setup_x_1.exe")
        self.assertEqual(g.scan_folder(self.d), [])


class Steam(unittest.TestCase):
    def test_appid_stable_and_unsigned_pair(self):
        u, s = g.shortcut_appid("Hollow Knight")
        self.assertEqual(g.shortcut_appid("Hollow Knight"), (u, s))
        self.assertTrue(u >= 0x80000000)
        self.assertEqual(s & 0xffffffff, u)

    def test_merge_keeps_others_byte_for_field(self):
        new = g.shortcut_entry(-5, "New", "/a b/c.exe", "/a b", "")
        out = g.vdf_load(g.merge_shortcuts(existing_shortcuts(), [new]))["shortcuts"]
        self.assertEqual(len(out), 2)
        self.assertEqual(out["0"]["appname"], "Google Chrome")
        self.assertEqual(out["0"]["custom"], "kept")
        self.assertEqual(out["0"]["LastPlayTime"], 1234)
        self.assertEqual(out["1"]["appname"], "New")
        self.assertEqual(out["1"]["Exe"], '"/a b/c.exe"')

    def test_merge_replaces_same_appid_keeps_playtime(self):
        new = g.shortcut_entry(-1293752665, "Renamed", "/x.exe", "/", "")
        out = g.vdf_load(g.merge_shortcuts(existing_shortcuts(), [new]))["shortcuts"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out["0"]["appname"], "Renamed")
        self.assertEqual(out["0"]["LastPlayTime"], 1234)

    def test_merge_into_missing_file(self):
        out = g.vdf_load(g.merge_shortcuts(b"", [g.shortcut_entry(-5, "A", "/a", "/", "")]))["shortcuts"]
        self.assertEqual(out["0"]["appname"], "A")

    def test_compat_tool_adds_only_our_block(self):
        out = g.set_compat_tool(CONFIG, 3000000000, "proton_experimental")
        block = ('\t\t\t\t\t"3000000000"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"proton_experimental"\n'
                 '\t\t\t\t\t\t"config"\t\t""\n\t\t\t\t\t\t"priority"\t\t"250"\n\t\t\t\t\t}\n')
        self.assertIn(block, out)
        self.assertEqual(out.replace(block, ""), CONFIG)

    def test_compat_tool_twice_keeps_one(self):
        once = g.set_compat_tool(CONFIG, 3000000000, "proton_experimental")
        self.assertEqual(g.set_compat_tool(once, 3000000000, "proton_experimental"), once)

    def test_compat_tool_missing_block_refused(self):
        with self.assertRaises(ValueError):
            g.set_compat_tool('"x"\n{\n}\n', 1, "t")


class Art(unittest.TestCase):
    ITEMS = [{"id": 1030300, "name": "Hollow Knight: Silksong", "type": "app"},
             {"id": 598190, "name": "Hollow Knight", "type": "dlc"},
             {"id": 367520, "name": "Hollow Knight", "type": "app"}]

    def test_exact_app_match_only(self):
        self.assertEqual(g.match_steam_app("Hollow Knight", self.ITEMS), 367520)
        self.assertIsNone(g.match_steam_app("Hollow", self.ITEMS))

    def test_match_ignores_marks_and_punctuation(self):
        items = [{"id": 285330, "name": "RollerCoaster Tycoon® 2: Triple Thrill Pack", "type": "app"}]
        self.assertEqual(g.match_steam_app("RollerCoaster Tycoon 2 Triple Thrill Pack", items), 285330)

    def test_only_real_images_count(self):
        self.assertEqual(g.image_ext(b"\xff\xd8\xff\xe0xx"), "jpg")
        self.assertEqual(g.image_ext(b"\x89PNG\r\n\x1a\nxx"), "png")
        self.assertIsNone(g.image_ext(b"<html>not found</html>"))

    def test_cover_svg_escapes_title_and_wraps(self):
        svg = g.cover_svg("Heroes of Might & Magic 3 Complete", b"x", "jpg")
        self.assertIn(">&amp; Magic 3<", svg)
        self.assertEqual(svg.count("<text"), 3)
        self.assertIn("image/jpeg", svg)

    def test_existing_art_is_kept(self):
        d = tempfile.mkdtemp()
        with open(f"{d}/99p.png", "wb") as f:
            f.write(b"mine")
        self.assertTrue(g.has_art(d, 99, "p"))
        self.assertFalse(g.has_art(d, 99, "_hero"))
        # wide art has no suffix, and must not be confused with the portrait cover
        self.assertFalse(g.has_art(d, 99, ""))


class GogLookup(unittest.TestCase):
    def test_parse_real_names(self):
        self.assertEqual(g.parse_setup("setup_hollow_knight_1.5.12620_(64bit)_(89718).exe"), ("hollow_knight", "1.5.12620"))
        self.assertEqual(g.parse_setup("setup_heroes_of_might_and_magic_3_complete_4.0_(3.2)_gog_0.1_(77075).exe"),
                         ("heroes_of_might_and_magic_3_complete", "4.0_(3.2)_gog_0.1"))
        self.assertEqual(g.parse_setup("setup_rollercoaster_tycoon2_2.0.0.6.exe"), ("rollercoaster_tycoon2", "2.0.0.6"))

    def test_catalog_match_is_exact(self):
        products = [{"id": 1, "slug": "hollow_knight_silksong", "title": "Hollow Knight: Silksong"},
                    {"id": 2, "slug": "hollow_knight", "title": "Hollow Knight"}]
        self.assertEqual(g.match_catalog("hollow_knight", products)["id"], 2)
        self.assertIsNone(g.match_catalog("hollow", products))
        heroes = [{"id": 3, "slug": "heroes_of_might_and_magic_3_complete_edition", "title": "Heroes of Might and Magic® 3: Complete"}]
        self.assertEqual(g.match_catalog("heroes_of_might_and_magic_3_complete", heroes)["id"], 3)
        rct = [{"id": 4, "slug": "rollercoaster_tycoon_2", "title": "RollerCoaster Tycoon® 2: Triple Thrill Pack"}]
        self.assertEqual(g.match_catalog("rollercoaster_tycoon2", rct)["id"], 4)

    def test_build_must_match_exactly(self):
        items = [{"version_name": "4.0"}, {"version_name": "4.0 (3.2) GOG 0.1", "link": "x"}]
        self.assertEqual(g.match_build("4.0_(3.2)_gog_0.1", items)["link"], "x")
        self.assertIsNone(g.match_build("2.0.0.6", [{"version_name": "2.01.043"}, {"version_name": ""}]))
        self.assertIsNone(g.match_build("", [{"version_name": ""}]))

    def test_size_counts_only_english_of_this_product(self):
        meta = {"depots": [
            {"productId": "1", "languages": ["*"], "size": 100}, {"productId": "1", "languages": ["en"], "size": 10},
            {"productId": "1", "languages": ["de"], "size": 999}, {"productId": "2", "languages": ["*"], "size": 999}]}
        self.assertEqual(g.depots_size(meta, 1), 110)
        self.assertIsNone(g.depots_size({"depots": []}, 1))

    def test_unknown_size_stays_unknown(self):
        game = {"slug": "x", "size": 500}
        state = {"games": {}}
        self.assertIsNone(g.install_size(game, state))
        self.assertEqual(g.needed_bytes(game, state), 500)  # the installer's own size, not a guess
        game["install_size"] = 900
        self.assertEqual(g.needed_bytes(game, state), 900)
        state["games"]["x"] = {"installed_size": 950}
        self.assertEqual(g.install_size(game, state), 950)  # measured beats published


class Smb(unittest.TestCase):
    def test_shares_from_real_listing(self):
        out = "IPC|IPC$|IPC Service\nDisk|print$|Printer Drivers\nDisk|games|\nDisk|iso|DVD BD ISOs\nDisk|Movies|\n"
        self.assertEqual(g.parse_shares(out), ["games", "iso", "Movies"])

    def test_folders_from_real_listing(self):
        out = ("  .                                   D        0  Thu Nov  5 15:36:34 2026\n"
               "  ..                                  D        0  Thu Nov  5 14:45:37 2026\n"
               "  Fallout 2 v1.02 GOG v1 GoG          D        0  Thu Nov  5 15:39:00 2026\n"
               "  Hollow Knight & Soundtracks v1.5.12459 GoG      D        0  Thu Nov  5 14:52:32 2026\n"
               "  .drm-write-test                     D        0  Thu Nov  5 16:00:00 2026\n"
               "  wavesafe-deck-bundle.zip            A 80089940  Thu Jun 11 21:17:01 2026\n"
               "\n\t\t3844550452 blocks of size 1024. 1417323944 blocks available\n")
        self.assertEqual(g.parse_folders(out), ["Fallout 2 v1.02 GOG v1 GoG", "Hollow Knight & Soundtracks v1.5.12459 GoG"])

    def test_fstab_needs_user_and_read_only(self):
        good = "//192.168.0.13/games /home/deck/gog\\040share cifs ro,guest,user,noauto 0 0\n"
        self.assertEqual(g.fstab_mountpoint("192.168.0.13", "games", good), "/home/deck/gog share")
        self.assertIsNone(g.fstab_mountpoint("192.168.0.13", "games", good.replace("ro,", "rw,")))
        self.assertIsNone(g.fstab_mountpoint("192.168.0.13", "games", good.replace("user,", "")))
        self.assertIsNone(g.fstab_mountpoint("192.168.0.13", "other", good))
        self.assertIsNone(g.fstab_mountpoint("192.168.0.13", "games", "#" + good))

    def test_labels_follow_source(self):
        cfg = dict(g.DEFAULT_CONFIG, source="smb")
        self.assertEqual(g.source_label(cfg), ("192.168.0.13", "games/gog"))
        self.assertEqual(g.source_label(dict(cfg, source="ssh")), ("192.168.0.13", "/mnt/4tba/games/gog"))


class SmbMount(unittest.TestCase):
    def test_share_without_fstab_line_is_refused(self):
        g.open = lambda *a, **k: io.StringIO("/dev/x /home ext4 defaults 0 2\n")
        try:
            root, err = g.mount_smb(dict(g.DEFAULT_CONFIG, source="smb"))
        finally:
            del g.open
        self.assertIsNone(root)
        self.assertIn("isn't set up", err)


class GogInfo(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def write(self, data):
        p = f"{self.d}/goggame-1.info"
        with open(p, "w", encoding="utf-8-sig") as f:
            json.dump(data, f)
        return p

    def test_primary_task(self):
        p = self.write({"name": "Game", "playTasks": [
            {"type": "URLTask", "link": "x"},
            {"isPrimary": True, "type": "FileTask", "path": "bin\\game.exe", "workingDir": "bin", "arguments": "-x"}]})
        t = g.parse_goggame(p)
        self.assertEqual(t["exe"], f"{self.d}/bin/game.exe")
        self.assertEqual(t["workdir"], f"{self.d}/bin")
        self.assertEqual(t["args"], "-x")

    def test_no_working_dir_uses_exe_folder(self):
        t = g.parse_goggame(self.write({"playTasks": [{"isPrimary": True, "path": "sub/g.exe"}]}))
        self.assertEqual(t["workdir"], f"{self.d}/sub")

    def test_no_tasks(self):
        self.assertIsNone(g.parse_goggame(self.write({"name": "DLC"})))


if __name__ == "__main__":
    unittest.main()
