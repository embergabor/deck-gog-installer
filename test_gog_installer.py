"""Tests for the parts that touch Steam's files or decide what gets installed."""
import io
import json
import os
import tempfile
import unittest

from unittest import mock

import gog_installer as g

_TMP = tempfile.mkdtemp()
g.INSTALLER_INFO_FILE, g.LOOKUP_FILE = f"{_TMP}/installer-info.json", f"{_TMP}/gog-lookup.json"


def version_resource(name, version):
    """The tail of a VS_VERSIONINFO block: UTF-16 key, padding, UTF-16 value."""
    out = b""
    for key, value in (("ProductName", name), ("ProductVersion", version)):
        out += (key + "\0").encode("utf-16-le") + b"\0\0" + (value + "\0").encode("utf-16-le") + b"\0\0"
    return out


def installer_exe(name, version):
    """A minimal PE with a .rsrc section holding the version strings, like a GOG setup exe."""
    import struct
    rsrc = b"\0" * 64 + version_resource(name, version)
    head = bytearray(0x400)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x80)
    head[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", head, 0x86, 2)
    struct.pack_into("<H", head, 0x94, 224)
    sec = 0x80 + 24 + 224
    struct.pack_into("<8sIIII", head, sec, b".text", 16, 0x1000, 16, 0x400)
    struct.pack_into("<8sIIII", head, sec + 40, b".rsrc", len(rsrc), 0x2000, len(rsrc), 0x410)
    return bytes(head) + b"\xcc" * 16 + rsrc + b"\0" * 1000  # installer data follows

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

    def test_identity_and_title_come_from_the_installer(self):
        os.makedirs(f"{self.d}/HoMM3 v4.0 GoG")
        with open(f"{self.d}/HoMM3 v4.0 GoG/setup_heroes_of_might_and_magic_3_complete_4.0_(3.2)_gog_0.1_(77075).exe", "wb") as f:
            f.write(installer_exe("Heroes of Might and Magic\u00ae 3 - Complete", "4.0 (3.2) GOG 0.1.[58018312193614395]"))
        game = g.scan_folder(self.d)[0]
        self.assertEqual((game["id"], game["slug"], game["folder"]),
                         ("heroes_of_might_and_magic_3_complete", "heroes_of_might_and_magic_3_complete", "HoMM3 v4.0 GoG"))
        self.assertEqual((game["title"], game["product_name"], game["version"]),
                         ("Heroes of Might and Magic 3 - Complete", "Heroes of Might and Magic 3 - Complete", "4.0 (3.2) GOG 0.1"))

    def test_cut_product_name_isnt_the_title(self):
        os.makedirs(f"{self.d}/Oblivion")
        with open(f"{self.d}/Oblivion/setup_the_elder_scrolls_iv_oblivion_goty_deluxe_1.2.0416_gog_v3_(1).exe", "wb") as f:
            f.write(installer_exe("The Elder Scrolls IV - Oblivion - Game of the Year Edition D", "1.2.0416 GOG v3"))
        game = g.scan_folder(self.d)[0]
        self.assertEqual(game["title"], "The Elder Scrolls IV - Oblivion - Game of the Year Edition")  # until GOG's title arrives
        self.assertEqual(g.whole_words(game["product_name"]), "The Elder Scrolls IV - Oblivion - Game of the Year Edition")

    def test_no_version_info_falls_back_to_file_name(self):
        self.add("Game", "setup_some_game_1.2.exe")
        game = g.scan_folder(self.d)[0]
        self.assertEqual((game["title"], game["version"], game["product_name"]), ("Some Game", "1.2", None))

    def test_folder_without_installer_skipped(self):
        self.add("Empty", "notes.txt")
        self.add(".hidden", "setup_x_1.exe")
        self.assertEqual(g.scan_folder(self.d), [])


class Removed(unittest.TestCase):
    def test_game_removed_in_steam_is_forgotten_others_kept(self):
        steam = tempfile.mkdtemp()
        state = {"games": {"gone": {"exe": "/nowhere/game.exe"}, "here": {"exe": "/nowhere/x.exe"}}}
        with mock.patch.object(g, "STEAM", steam), mock.patch.object(g, "save_state") as save:
            os.makedirs(g.prefix_dir("here"))
            self.assertEqual(g.forget_removed(state), ["gone"])
        self.assertEqual(list(state["games"]), ["here"])
        save.assert_called_once()


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
    def steam(self, items):
        return mock.patch.object(g, "steam_search", lambda term: [i for i in items if i.get("type", "app") == "app"])

    def test_steam_match_exact_app_only(self):
        items = [{"id": 1030300, "name": "Hollow Knight: Silksong"}, {"id": 598190, "name": "Hollow Knight", "type": "dlc"},
                 {"id": 367520, "name": "Hollow Knight"}]
        with self.steam(items):
            self.assertEqual(g.steam_match(["Hollow Knight"]), 367520)
            self.assertIsNone(g.steam_match(["Hollow"]))

    def test_steam_match_ignores_marks_and_punctuation(self):
        with self.steam([{"id": 285330, "name": "RollerCoaster Tycoon\u00ae 2: Triple Thrill Pack"}]):
            self.assertEqual(g.steam_match(["RollerCoaster Tycoon 2 Triple Thrill Pack"]), 285330)

    def test_gog_edition_suffix_may_go_installer_name_may_not(self):
        with self.steam([{"id": 292030, "name": "The Witcher 3: Wild Hunt"}]):
            self.assertEqual(g.steam_match(["The Witcher 3 - Wild Hunt - Complete Edition"],
                                           "The Witcher 3: Wild Hunt \u2014 Remastered"), 292030)
        with self.steam([{"id": 228280, "name": "Baldur's Gate"}]):
            self.assertIsNone(g.steam_match(["Baldur's Gate - The Original Saga"]))

    def test_loose_steam_match_must_be_the_only_one(self):
        with self.steam([{"id": 1, "name": "Disco Elysium - The Final Cut"}]):
            self.assertEqual(g.steam_match(["Disco Elysium"]), 1)
        with self.steam([{"id": 1, "name": "Fallout - A"}, {"id": 2, "name": "Fallout - B"}]):
            self.assertIsNone(g.steam_match(["Fallout"]))

    def test_only_real_images_count(self):
        self.assertEqual(g.image_ext(b"\xff\xd8\xff\xe0xx"), "jpg")
        self.assertEqual(g.image_ext(b"\x89PNG\r\n\x1a\nxx"), "png")
        self.assertIsNone(g.image_ext(b"<html>not found</html>"))

    def test_cover_svg_escapes_title_and_wraps(self):
        svg = g.cover_svg("Heroes of Might & Magic 3 Complete", b"x", "jpg")
        self.assertIn(">&amp; Magic 3<", svg)
        self.assertEqual(svg.count("<text"), 3)
        self.assertIn("image/jpeg", svg)

    def test_made_cover_is_replaced_by_real_box_art_hand_set_art_is_not(self):
        steam = tempfile.mkdtemp()
        grid = f"{steam}/userdata/1/config/grid"
        os.makedirs(grid)
        state = {"games": {"old": {"title": "Old", "gog_id": "", "steam_appid": 0},  # no art recorded: older version
                           "mine": {"title": "Mine", "steam_appid": 0, "cover_vertical": "https://x/c.jpg"}}}
        old, mine = g.shortcut_appid("old")[0], g.shortcut_appid("mine")[0]
        for name in (f"{old}p.png", f"{mine}p.png"):
            with open(f"{grid}/{name}", "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nmade")
        jpg = b"\xff\xd8\xffbox art"
        with mock.patch.object(g, "STEAM", steam), mock.patch.object(g, "save_state"), \
                mock.patch.object(g, "self_art"), mock.patch.object(g, "refresh_from_gog_info"), \
                mock.patch.object(g, "make_cover", return_value=False), \
                mock.patch.object(g, "gog_data_for_installed", side_effect=lambda x: x.setdefault("cover_vertical", "https://x/o.jpg")), \
                mock.patch.object(g, "http_get", return_value=jpg):
            g.fetch_all_art(state, log=lambda m: None)
        self.assertIn(f"{old}p.jpg", os.listdir(grid))       # older version's made cover replaced
        self.assertNotIn(f"{old}p.png", os.listdir(grid))
        self.assertIn(f"{mine}p.png", os.listdir(grid))       # a newer entry's portrait isn't touched

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
        self.assertEqual(g.match_catalog(["hollow_knight"], products)["id"], 2)
        self.assertIsNone(g.match_catalog(["hollow"], products))
        heroes = [{"id": 3, "slug": "heroes_of_might_and_magic_3_complete_edition", "title": "Heroes of Might and Magic® 3: Complete"}]
        self.assertEqual(g.match_catalog(["heroes_of_might_and_magic_3_complete"], heroes)["id"], 3)
        rct = [{"id": 4, "slug": "rollercoaster_tycoon_2", "title": "RollerCoaster Tycoon® 2: Triple Thrill Pack"}]
        self.assertEqual(g.match_catalog(["rollercoaster_tycoon2"], rct)["id"], 4)

    def test_loose_rules(self):
        wanted = {g.normalize_title("Gabriel Knight 3")}
        self.assertTrue(g.loose_match("Gabriel Knight 3: Blood of the Sacred, Blood of the Damned", wanted))
        self.assertFalse(g.loose_match("Gabriel Knight 2", wanted))
        self.assertEqual(g.base_title("The Witcher 3: Wild Hunt \u2014 Remastered"), "The Witcher 3: Wild Hunt")
        self.assertEqual(g.search_terms("The Witcher 3 - Wild Hunt - Complete Edition"),
                         ["The Witcher 3 - Wild Hunt - Complete Edition", "The Witcher 3", "The Witcher 3 Wild"])
        self.assertIsNone(g.single([1, 2]))

    def test_vertical_cover_url(self):
        self.assertEqual(g.vertical_cover("https://images.gog-statics.com/abc.jpg"),
                         "https://images.gog-statics.com/abc_glx_vertical_cover.jpg")
        self.assertEqual(g.vertical_cover("https://x/abc_glx_vertical_cover.jpg"), "https://x/abc_glx_vertical_cover.jpg")
        self.assertIsNone(g.vertical_cover(None))

    def test_gogdb_rows(self):
        page = ('<tr><td class="col-id"><a href="/product/1207659070">1207659070</a></td><td class="col-type">Game</td>'
                '<td class="col-name prod-name"><a href="/product/1207659070">The Chronicles of Riddick&#8482;: '
                'Assault on Dark Athena</a></td></tr>')
        self.assertEqual(g.parse_gogdb(page), [("1207659070", "The Chronicles of Riddick: Assault on Dark Athena")])

    def test_version_info(self):
        info = g.parse_version_info(version_resource("The Witcher 3 - Wild Hunt - Complete Edition", "4.04b.[57606063263997132]"))
        self.assertEqual(info, {"name": "The Witcher 3 - Wild Hunt - Complete Edition", "version": "4.04b"})
        self.assertEqual(g.parse_version_info(b"nothing here"), {"name": None, "version": None})

    def test_cut_names(self):
        self.assertTrue(g.name_is_cut("The Elder Scrolls IV - Oblivion - Game of the Year Edition D"))
        self.assertEqual(g.whole_words("The Elder Scrolls IV - Oblivion - Game of the Year Edition D"),
                         "The Elder Scrolls IV - Oblivion - Game of the Year Edition")
        self.assertEqual(g.whole_words("Baldur's Gate - The Original Saga - E"), "Baldur's Gate - The Original Saga")

    def test_installer_name_beats_gog_title_unless_cut(self):
        name = "The Witcher 3 - Wild Hunt - Complete Edition"
        game = {"title": name, "product_name": name}
        g.apply_lookup(game, {"id": "1", "title": "The Witcher 3: Wild Hunt \u2014 Remastered", "steam_appid": 292030})
        self.assertEqual((game["title"], game["gog_id"], game["steam_appid"]),
                         ("The Witcher 3 - Wild Hunt - Complete Edition", "1", 292030))
        cut = {"title": "x", "product_name": "The Elder Scrolls IV - Oblivion - Game of the Year Edition D"}
        g.apply_lookup(cut, {"id": "2", "title": "The Elder Scrolls IV: Oblivion Game of the Year Edition Deluxe"})
        self.assertEqual(cut["title"], "The Elder Scrolls IV: Oblivion Game of the Year Edition Deluxe")
        g.apply_lookup(cut, {})  # no GOG match: the cut name back to whole words
        self.assertEqual(cut["title"], "The Elder Scrolls IV - Oblivion - Game of the Year Edition")

    def test_delisted_game_found_through_gogdb(self):
        riddick = {"id": "1207659070", "title": "The Chronicles of Riddick: Assault on Dark Athena",
                   "cover_vertical": "v", "cover_horizontal": "h", "logo": "l"}
        with mock.patch.object(g, "catalog_match", return_value=None), \
                mock.patch.object(g, "gogdb_match", side_effect=lambda t, n, r: None if r else "1207659070"), \
                mock.patch.object(g, "game_by_id", return_value=dict(riddick)), \
                mock.patch.object(g, "http_get", side_effect=OSError("no builds")), \
                mock.patch.object(g, "steam_match", return_value=None):
            info = g.gog_lookup("setup_chronicles_of_riddick_2.0.0.10.exe",
                                "The Chronicles of Riddick - Assault on Dark Athena")
        self.assertEqual((info["id"], info["cover_vertical"], info["steam_appid"], info["install_size"]),
                         ("1207659070", "v", 0, None))

    def test_cut_name_parts_only_in_the_loose_pass(self):
        seen = []

        def catalog(term, names, relaxed):
            seen.append((relaxed, tuple(names)))
            hit = relaxed and g.loose_match("The Elder Scrolls IV: Oblivion - Game of the Year Edition Deluxe",
                                            {g.normalize_title(n) for n in names})
            return {"id": "1242989820", "title": "The Elder Scrolls IV: Oblivion - Game of the Year Edition Deluxe"} if hit else None
        with mock.patch.object(g, "catalog_match", side_effect=catalog), mock.patch.object(g, "gogdb_match", return_value=None), \
                mock.patch.object(g, "http_get", side_effect=OSError), mock.patch.object(g, "steam_match", return_value=None):
            info = g.gog_lookup("setup_the_elder_scrolls_iv_oblivion_goty_deluxe_1.2.0416_gog_v3_(1).exe",
                                "The Elder Scrolls IV - Oblivion - Game of the Year Edition D")
        self.assertEqual(info["id"], "1242989820")
        self.assertTrue(all("The Elder Scrolls IV - Oblivion" not in names for relaxed, names in seen if not relaxed))

    def test_offline_lookup_isnt_remembered_as_no_match(self):
        with mock.patch.object(g, "catalog_match", side_effect=OSError("offline")), \
                mock.patch.object(g, "gogdb_match", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                g.gog_lookup("setup_fallout_2_1.02_gog_v1_(77792).exe", "Fallout 2")
        with mock.patch.object(g, "catalog_match", return_value=None), mock.patch.object(g, "gogdb_match", return_value=None):
            self.assertEqual(g.gog_lookup("setup_unknown_1.0.exe"), {})

    def test_build_must_match_exactly(self):
        items = [{"version_name": "4.0"}, {"version_name": "4.0 (3.2) GOG 0.1", "link": "x"}]
        self.assertEqual(g.match_build("4.0_(3.2)_gog_0.1", items)["link"], "x")
        self.assertIsNone(g.match_build("2.0.0.6", [{"version_name": "2.01.043"}, {"version_name": ""}]))
        self.assertIsNone(g.match_build("", [{"version_name": ""}]))
        self.assertIsNone(g.match_build(None, [{"version_name": ""}]))

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

    def test_url_is_encoded(self):
        self.assertEqual(g.smb_url("192.168.0.13", "games", "gog"), "smb://192.168.0.13/games/gog")
        self.assertEqual(g.smb_url("s", "games", "", "Hollow Knight & Soundtracks/setup_(64bit).exe"),
                         "smb://s/games/Hollow%20Knight%20%26%20Soundtracks/setup_%2864bit%29.exe")

    def test_stat_round_trip(self):
        buf = g.build_stat(False, 1241037720, 1762350000)
        self.assertEqual(len(buf), g.STAT_SIZE)
        self.assertEqual(g.parse_stat(buf), (False, 1241037720, 1762350000))
        self.assertEqual(g.parse_stat(g.build_stat(True, 4096, 5))[0], True)

    def test_stat_is_read_only(self):
        import struct
        mode = struct.unpack_from("<I", g.build_stat(False, 1, 1), 24)[0]
        self.assertEqual(mode & 0o222, 0)
        self.assertEqual(mode & 0o444, 0o444)

    def test_labels(self):
        self.assertEqual(g.source_label({"type": "smb", "server": "192.168.0.13", "share": "games", "folder": "gog"}),
                         ("SMB", "192.168.0.13", "games/gog"))
        self.assertEqual(g.source_label({"type": "usb", "label": "GAMES", "folder": ""}), ("USB", "GAMES", "(top of the drive)"))


LSBLK = json.dumps({"blockdevices": [
    {"path": "/dev/nvme0n1", "pkname": None, "type": "disk", "uuid": None, "label": None, "fstype": None, "tran": "nvme",
     "hotplug": False, "size": 2000000000000, "mountpoint": None},
    {"path": "/dev/nvme0n1p8", "pkname": "nvme0n1", "type": "part", "uuid": "home", "label": "home", "fstype": "ext4",
     "tran": None, "hotplug": False, "size": 1900000000000, "mountpoint": "/home"},
    {"path": "/dev/sda", "pkname": None, "type": "disk", "uuid": None, "label": None, "fstype": None, "tran": "usb",
     "hotplug": True, "size": 64000000000, "mountpoint": None},
    {"path": "/dev/sda1", "pkname": "sda", "type": "part", "uuid": "1234-ABCD", "label": "GAMES", "fstype": "exfat",
     "tran": None, "hotplug": True, "size": 63999000000, "mountpoint": None},
    {"path": "/dev/mmcblk0", "pkname": None, "type": "disk", "uuid": None, "label": None, "fstype": None, "tran": None,
     "hotplug": False, "size": 512000000000, "mountpoint": None},
    {"path": "/dev/mmcblk0p1", "pkname": "mmcblk0", "type": "part", "uuid": "sd-uuid", "label": None, "fstype": "ext4",
     "tran": None, "hotplug": False, "size": 512000000000, "mountpoint": "/run/media/deck/sd"},
    {"path": "/dev/sdb1", "pkname": "sdb", "type": "part", "uuid": "swap", "label": None, "fstype": "swap",
     "tran": None, "hotplug": True, "size": 1, "mountpoint": None}]})


class Sources(unittest.TestCase):
    def test_migrate_drops_ssh(self):
        cfg = {"sources": [{"type": "ssh", "host": "h", "path": "/p"}, {"type": "usb", "uuid": "u", "label": "L", "folder": ""}]}
        self.assertEqual(g.migrate_config(cfg), {"sources": [cfg["sources"][1]]})
        self.assertEqual(g.migrate_config({"host": "h", "path": "/p"}), {"sources": []})
        self.assertEqual(g.migrate_config({}), {"sources": []})

    def test_migrate_old_smb_choice(self):
        old = {"source": "smb", "smb_server": "s", "smb_share": "games", "smb_folder": "gog", "host": "x"}
        self.assertEqual(g.migrate_config(old)["sources"],
                         [{"type": "smb", "server": "s", "share": "games", "folder": "gog"}])

    def test_new_config_passes_through(self):
        cfg = {"sources": [{"type": "usb", "uuid": "u", "label": "L", "folder": "gog"}]}
        self.assertEqual(g.migrate_config(cfg), cfg)

    def test_drives_are_removable_partitions_only(self):
        drives = g.parse_drives(LSBLK)
        self.assertEqual([d["dev"] for d in drives], ["/dev/sda1", "/dev/mmcblk0p1"])
        self.assertEqual(drives[0]["fstype"], "exfat")
        self.assertEqual(drives[1]["mountpoint"], "/run/media/deck/sd")
        self.assertEqual(g.drive_name(drives[1]), "512.0 GB drive")

    def test_udisks_output(self):
        self.assertEqual(g.parse_udisks_mount("Mounted /dev/sda1 at /run/media/deck/GAMES\n"), "/run/media/deck/GAMES")
        self.assertEqual(g.parse_udisks_mount("Mounted /dev/sda1 at /run/media/deck/My Games.\n"),
                         "/run/media/deck/My Games")
        self.assertIsNone(g.parse_udisks_mount("Error mounting /dev/sda1: Not authorized"))

    SMB = {"type": "smb", "server": "s", "share": "games", "folder": "gog"}
    USB = {"type": "usb", "uuid": "u", "label": "L", "folder": "gog"}
    HK = "setup_hollow_knight_1.5.12620_(64bit)_(89718).exe"

    @staticmethod
    def game(folder, setup, title=None, version=None):
        return {"id": g.game_id(setup), "slug": g.game_id(setup), "folder": folder, "setup": setup,
                "title": title or folder, "version": version or g.parse_setup(setup)[1], "root": "/" + folder}

    def test_same_build_prefers_usb_whatever_the_order(self):
        merged = g.merge_games([(self.SMB, [self.game("Hollow Knight", self.HK)]), (self.USB, [self.game("HK copy", self.HK)])])
        self.assertEqual([(x["source"], x["folder"], x["slug"]) for x in merged], [("USB", "HK copy", "hollow_knight")])

    def test_newest_build_wins_over_usb(self):
        merged = g.merge_games([(self.USB, [self.game("HK", "setup_hollow_knight_1.5.9999.exe")]),
                                (self.SMB, [self.game("Hollow Knight", "setup_hollow_knight_1.5.12620.exe")])])
        self.assertEqual([(x["source"], x["version"]) for x in merged], [("SMB", "1.5.12620")])
        self.assertGreater(g.version_key("1.10"), g.version_key("1.9"))

    def test_folder_names_dont_make_games(self):
        merged = g.merge_games([(self.SMB, [self.game("Same name", "setup_fallout_2_1.02.exe", "Fallout 2")]),
                                (self.USB, [self.game("Same name", "setup_heroes_3.exe", "Heroes 3")])])
        self.assertEqual([x["title"] for x in merged], ["Fallout 2", "Heroes 3"])

    def test_install_found_by_game_id_after_folder_rename(self):
        installed = {"heroes_of_might_and_magic_3_complete": {"game_id": "heroes_of_might_and_magic_3_complete"}}
        h3 = "setup_heroes_of_might_and_magic_3_complete_4.0_(28740).exe"
        self.assertEqual(g.merge_games([(self.SMB, [self.game("Anything", h3)])], installed)[0]["slug"],
                         "heroes_of_might_and_magic_3_complete")

    def test_older_installs_keep_their_folder_keyed_identity(self):
        old = {"Hollow Knight & Soundtracks v1.5.12459 GoG": {"title": "Hollow Knight"}}
        merged = g.merge_games([(self.USB, [self.game("Hollow Knight & Soundtracks v1.5.12459 GoG", self.HK)])], old)
        self.assertEqual(merged[0]["slug"], "Hollow Knight & Soundtracks v1.5.12459 GoG")
        by_setup = {"HoMM3 v4.0 GoG": {"title": "x", "setup": "setup_heroes_of_might_and_magic_3_complete_4.0_(1).exe"}}
        h3 = self.game("Renamed", "setup_heroes_of_might_and_magic_3_complete_4.1_(2).exe")
        self.assertEqual(g.merge_games([(self.SMB, [h3])], by_setup)[0]["slug"], "HoMM3 v4.0 GoG")
        by_title = {"HoMM3 v4.0 GoG": {"title": "Heroes of Might and Magic 3 Complete"}}
        h3 = self.game("Renamed", "setup_heroes_of_might_and_magic_3_complete_4.0.exe", "Heroes of Might and Magic 3 - Complete")
        self.assertEqual(g.merge_games([(self.SMB, [h3])], by_title)[0]["slug"], "HoMM3 v4.0 GoG")

    def test_older_install_titled_from_the_file_name(self):
        installed = {"RollerCoaster.Tycoon.2.Triple.Thrill.Pack.v2.0.0.6-GOG": {"title": "Rollercoaster Tycoon2"}}
        rct = self.game("RollerCoaster.Tycoon.2.Triple.Thrill.Pack", "setup_rollercoaster_tycoon2_2.0.0.6.exe",
                        "RollerCoaster Tycoon 2 Triple Thrill Pack")
        self.assertEqual(g.merge_games([(self.SMB, [rct])], installed)[0]["slug"],
                         "RollerCoaster.Tycoon.2.Triple.Thrill.Pack.v2.0.0.6-GOG")

    def test_two_installs_of_one_game_prefer_this_folder(self):
        installed = {"Heroes of Might and Magic 3 - Complete": {"title": "Heroes of Might and Magic 3 Complete"},
                     "Heroes of Might and Magic 3 - Complete v4.0 (3.2) GOG 0.1 GoG": {"title": "Heroes of Might and Magic 3 Complete"}}
        h3 = self.game("Heroes of Might and Magic 3 - Complete", "setup_heroes_of_might_and_magic_3_complete_4.0.exe",
                       "Heroes of Might and Magic 3 - Complete")
        self.assertEqual(g.merge_games([(self.SMB, [h3])], installed)[0]["slug"], "Heroes of Might and Magic 3 - Complete")

    def test_title_match_only_for_installs_without_installer_record(self):
        installed = {"HK": {"title": "Hollow Knight", "game_id": "hollow_knight_other"}}
        game = self.game("Hollow Knight", "setup_hollow_knight_1.5.12620.exe", "Hollow Knight")
        self.assertEqual(g.merge_games([(self.SMB, [game])], installed)[0]["slug"], "hollow_knight")

    def test_unfinished_sources_are_dropped(self):
        self.assertTrue(g.is_complete(self.SMB))
        self.assertFalse(g.is_complete(dict(self.SMB, share="")))
        self.assertFalse(g.is_complete({"type": "usb", "uuid": "", "label": "", "folder": ""}))
        self.assertFalse(g.is_complete({"type": "ssh", "host": "h", "path": "/p"}))

    def test_mount_dir_per_source(self):
        a = {"type": "smb", "server": "s", "share": "games", "folder": "gog"}
        self.assertEqual(g.source_mount_dir(a), g.source_mount_dir(dict(a)))
        self.assertNotEqual(g.source_mount_dir(a), g.source_mount_dir(dict(a, folder="other")))


class Fixes(unittest.TestCase):
    def test_canonical_forms(self):
        self.assertEqual(g.canonical_launch_option("WINEDLLOVERRIDES='ddraw.dll=n,b' %command%"), 'WINEDLLOVERRIDES="ddraw=n,b"')
        self.assertEqual(g.canonical_launch_option('WINEDLLOVERRIDES="DDraw=n" %command%'), 'WINEDLLOVERRIDES="ddraw=n"')
        self.assertEqual(g.canonical_launch_option("PROTON_USE_WINED3D=1 DXVK_ASYNC=1 %command% -vulkan"),
                         "DXVK_ASYNC=1 PROTON_USE_WINED3D=1 -vulkan")
        self.assertEqual(g.canonical_launch_option("-force-opengl"), "-force-opengl")

    def test_unsafe_options_are_rejected(self):
        for bad in ("gamemoderun %command%", "LD_PRELOAD=/x.so %command%", "mangohud %command%",
                    '"C:\\Program Files\\Mod.exe" %command%', "/usr/bin/env -u LD_PRELOAD %command%",
                    "SteamDeck=0 %command%", "%command%", "", "PROTON_USE_WINED3D=1 'unterminated"):
            self.assertIsNone(g.canonical_launch_option(bad), bad)

    def report(self, lo, verdict="yes", age=0, now=2_000_000_000):
        return {"timestamp": now - age, "responses": {"verdict": verdict, "launchOptions": lo}}

    def test_suggestions_count_working_recent_reports(self):
        now = 2_000_000_000
        reports = [self.report("WINEDLLOVERRIDES='ddraw.dll=n,b' %command%")] * 3 + \
            [self.report('WINEDLLOVERRIDES="ddraw=n,b" %command%')] + \
            [self.report("PROTON_USE_WINED3D=1 %command%")] + \
            [self.report("PROTON_USE_WINED3D=1 %command%", verdict="no")] * 5 + \
            [self.report("PROTON_NO_FSYNC=1 %command%", age=4 * 365 * 86400)] * 4 + \
            [self.report("gamemoderun %command%")] * 9
        self.assertEqual(g.suggest_fixes(reports, now), [('WINEDLLOVERRIDES="ddraw=n,b"', 4)])

    def test_compose_keeps_gog_args_when_nothing_chosen(self):
        self.assertEqual(g.compose_launch_options([], "-lang en"), "-lang en")
        self.assertEqual(g.compose_launch_options([], ""), "")

    def test_compose_env_command_args(self):
        out = g.compose_launch_options(['WINEDLLOVERRIDES="ddraw=n,b"', "PROTON_USE_WINED3D=1", "-vulkan",
                                        "WINEDLLOVERRIDES=dinput8=n,b"], "-lang en")
        self.assertEqual(out, 'PROTON_USE_WINED3D=1 WINEDLLOVERRIDES="ddraw=n,b;dinput8=n,b" %command% -vulkan -lang en')

    def test_compose_log(self):
        out = g.compose_launch_options([], "", "/home/deck/.local/share/gog-installer/logs/123")
        self.assertEqual(out, "PROTON_LOG=1 PROTON_LOG_DIR=/home/deck/.local/share/gog-installer/logs/123 %command%")

    def test_pending_includes_updated_games(self):
        state = {"games": {"a": {"in_steam": True}, "b": {"in_steam": True, "needs_update": True}, "c": {}}}
        self.assertEqual(sorted(g.pending_games(state)), ["b", "c"])


class ProtonDb(unittest.TestCase):
    def test_stream_across_chunks_and_multibyte(self):
        reports = [{"app": {"steam": {"appId": str(i)}, "title": "Pokémon ✓ {\"app\": {"},
                    "responses": {"launchOptions": "%command% -x"}} for i in range(50)]
        for indent in (None, 2):  # the real export is pretty-printed with indent 2
            data = json.dumps(reports, ensure_ascii=False, indent=indent).encode()
            out = list(g.report_stream(io.BytesIO(data), chunk=7))
            self.assertEqual([r["app"]["steam"]["appId"] for r in out], [str(i) for i in range(50)])
            self.assertEqual(out[3]["app"]["title"], reports[3]["app"]["title"])

    def test_index_from_stream(self):
        now = 2_000_000_000
        r = lambda aid, lo: {"app": {"steam": {"appId": aid}}, "timestamp": now,
                             "responses": {"verdict": "yes", "launchOptions": lo}}
        data = json.dumps([r("38410", "WINEDLLOVERRIDES='ddraw.dll=n,b' %command%")] * 3 +
                          [r("367520", "-force-opengl")]).encode()
        self.assertEqual(g.build_protondb_index(io.BytesIO(data), now, log=lambda m: None),
                         {"38410": [['WINEDLLOVERRIDES="ddraw=n,b"', 3]]})

    def test_latest_export(self):
        self.assertEqual(g.latest_export(["reports_dec1_2018.tar.gz", "reports_nov1_2026.tar.gz",
                                          "reports_sep2_2026.tar.gz", "README.md"]), "reports_nov1_2026.tar.gz")

    def test_proton_names(self):
        self.assertEqual(g.valve_tool_name("Proton - Experimental"), "proton_experimental")
        self.assertEqual(g.valve_tool_name("Proton 10.0"), "proton_10")
        self.assertEqual(g.valve_tool_name("Proton 5.13"), "proton_513")
        self.assertIsNone(g.valve_tool_name("Proton Voice Files"))
        self.assertEqual(g.valve_tool_name("Proton 9.0 (Beta)"), "proton_9")
        vdf = '''"compatibilitytools" { "compat_tools" {
          "GE-Proton10-1" { "install_path" "." "display_name" "GE" "from_oslist" "windows" "to_oslist" "linux" }
          "LegacyRuntime" { "install_path" "." "from_oslist" "linux" "to_oslist" "linux" } } }'''
        self.assertEqual(g.custom_tool_names(vdf), ["GE-Proton10-1"])
        commented = '''"compatibilitytools"
{
  "compat_tools"
  {
    "ULWGL-Proton-8.0-5-3" // Internal name of this tool
    {
      // - This manifest can be placed directly in compatibilitytools.d
      "install_path" "."
      "from_oslist"  "windows"
      "to_oslist"    "linux"
    }
  }
}'''
        self.assertEqual(g.custom_tool_names(commented), ["ULWGL-Proton-8.0-5-3"])


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



def make_pe(imports):
    """A minimal 32-bit PE whose import table lists {dll: [functions]}."""
    import struct
    pe, opt_size, sec_va, sec_raw = 0x40, 224, 0x1000, 0x200
    body = bytearray()
    names, thunks = {}, {}
    desc_size = 20 * (len(imports) + 1)
    body += b"\0" * desc_size
    for dll, fns in imports.items():
        offs = []
        for fn in fns:
            offs.append(len(body))
            body += b"\0\0" + fn.encode() + b"\0"
        thunks[dll] = len(body)
        for o in offs:
            body += struct.pack("<I", sec_va + o)
        body += b"\0" * 4
        names[dll] = len(body)
        body += dll.encode() + b"\0"
    for i, dll in enumerate(imports):
        struct.pack_into("<5I", body, i * 20, sec_va + thunks[dll], 0, 0, sec_va + names[dll], sec_va + thunks[dll])
    head = bytearray(sec_raw)
    struct.pack_into("<I", head, 0x3C, pe)
    head[pe:pe + 4] = b"PE\0\0"
    struct.pack_into("<H", head, pe + 6, 1)
    struct.pack_into("<H", head, pe + 20, opt_size)
    struct.pack_into("<H", head, pe + 24, 0x10B)
    struct.pack_into("<I", head, pe + 24 + 104, sec_va)
    struct.pack_into("<8sIIII", head, pe + 24 + opt_size, b".idata", len(body), sec_va, len(body), sec_raw)
    return bytes(head + body)


class DDrawCompat(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.exe = os.path.join(self.dir, "Heroes3.exe")

    def write(self, imports, wrapper=b"Loading DDrawCompat 0.5"):
        with open(self.exe, "wb") as f:
            f.write(make_pe(imports))
        with open(os.path.join(self.dir, "xdd.dll"), "wb") as f:
            f.write(wrapper)

    def test_reads_imports(self):
        self.write({"KERNEL32.dll": ["ExitProcess"], "xdd.dll": ["DirectDrawCreate"]})
        with open(self.exe, "rb") as f:
            imps = g.pe_imports(f.read())
        self.assertEqual([(n, f) for n, _, f in imps], [("KERNEL32.dll", ["ExitProcess"]), ("xdd.dll", ["DirectDrawCreate"])])

    def test_bypass_on_and_off(self):
        self.write({"KERNEL32.dll": ["ExitProcess"], "xdd.dll": ["DirectDrawCreate"]})
        with open(self.exe, "rb") as f:
            original = f.read()
        self.assertEqual(g.ddrawcompat_state(self.exe), "off")
        g.set_ddrawcompat_bypass(self.exe, True)
        self.assertEqual(g.ddrawcompat_state(self.exe), "on")
        with open(self.exe, "rb") as f:
            self.assertEqual([n for n, _, _ in g.pe_imports(f.read())], ["KERNEL32.dll", "ddraw"])
        g.set_ddrawcompat_bypass(self.exe, True)  # twice keeps the real original
        with open(self.exe + ".gog", "rb") as f:
            self.assertEqual(f.read(), original)
        g.set_ddrawcompat_bypass(self.exe, False)
        with open(self.exe, "rb") as f:
            self.assertEqual(f.read(), original)
        self.assertFalse(os.path.exists(self.exe + ".gog"))
        self.assertEqual(g.ddrawcompat_state(self.exe), "off")

    def test_other_wrappers_and_plain_games_left_alone(self):
        self.write({"xdd.dll": ["DirectDrawCreate"]}, wrapper=b"some other dll")
        self.assertIsNone(g.ddrawcompat_state(self.exe))
        self.write({"xdd.dll": ["DirectDrawCreate", "SomethingElse"]})
        self.assertIsNone(g.ddrawcompat_state(self.exe))
        self.write({"DDRAW.dll": ["DirectDrawCreate"]})
        self.assertIsNone(g.ddrawcompat_state(self.exe))
        with open(self.exe, "wb") as f:
            f.write(b"MZ not a pe" + b"\0" * 64)
        self.assertIsNone(g.ddrawcompat_state(self.exe))


if __name__ == "__main__":
    unittest.main()
