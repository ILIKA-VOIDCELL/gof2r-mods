"""Tests of tools/catalog.py: python -m unittest discover -s tools"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402


def make_zip(files):
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def png(w=64, h=36):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


class InspectTests(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "paths", []):
            os.remove(p)

    def zip(self, files):
        self.paths = getattr(self, "paths", [])
        self.paths.append(make_zip(files))
        return self.paths[-1]

    def test_a_mod_at_the_root(self):
        info = catalog.inspect_zip(self.zip({
            "mod.json": '{ // a comment\n "id": "my_mod", "name": {"en": "Mine", "de": "Meins"}, "version": "1.2", '
                        '"author": "Ann", "dependencies": ["base>=1.0"], }',
            "items.json": "[]", "preview.png": png()}))
        self.assertEqual("my_mod", info["id"])
        self.assertEqual({"en": "Mine", "de": "Meins"}, info["name"])
        self.assertEqual("1.2", info["version"])
        self.assertEqual(["base>=1.0"], info["dependencies"])
        self.assertFalse(info["scripts"])
        self.assertIsNotNone(info["preview"])
        self.assertEqual(3, info["files"])

    def test_a_mod_in_one_top_folder(self):
        info = catalog.inspect_zip(self.zip({"my_mod/mod.json": '{"id": "my_mod"}', "my_mod/scripts/main.lua": "--"}))
        self.assertEqual("my_mod", info["id"])
        self.assertEqual("1.0", info["version"], "the game's default")
        self.assertTrue(info["scripts"])
        self.assertIsNone(info["preview"])

    def test_refused(self):
        cases = {
            "no mod.json": {"a/b/mod.json": '{"id": "x"}'},
            "may only use": {"mod.json": '{"id": "Bad Id"}'},
            "unsafe path": {"mod.json": '{"id": "x"}', "../evil.txt": "x"},
            ".exe files": {"mod.json": '{"id": "x"}', "tool.exe": "MZ"},
            "can't be read": {"mod.json": '{"id": '},
            "dependencies": {"mod.json": '{"id": "x", "dependencies": "y"}'},
        }
        for why, files in cases.items():
            with self.subTest(why):
                with self.assertRaises(catalog.ModError) as e:
                    catalog.inspect_zip(self.zip(files))
                self.assertIn(why, str(e.exception))

    def test_not_a_zip(self):
        fd, path = tempfile.mkstemp()
        os.write(fd, b"<html>")
        os.close(fd)
        try:
            with self.assertRaises(catalog.ModError):
                catalog.inspect_zip(path)
        finally:
            os.remove(path)


class RuleTests(unittest.TestCase):
    def test_versions_compare_like_the_game(self):
        self.assertTrue(catalog.version_at_least("1.10", "1.9"))
        self.assertFalse(catalog.version_at_least("1.9", "1.10"))
        self.assertTrue(catalog.version_at_least("1.0", "1"))
        self.assertTrue(catalog.version_at_least("2.0-beta", "2.0-alpha"))

    def test_updates_raise_the_version(self):
        entry = {"version": "1.2", "sha256": "aa"}
        self.assertIsNone(catalog.check_update(None, {"version": "0.1"}, "bb"), "a new mod")
        self.assertIsNone(catalog.check_update(entry, {"version": "1.3"}, "bb"))
        self.assertIsNone(catalog.check_update(entry, {"version": "1.2"}, "aa"), "the same files")
        self.assertIn("raise", catalog.check_update(entry, {"version": "1.2"}, "bb"))
        self.assertIn("older", catalog.check_update(entry, {"version": "1.1"}, "bb"))

    def test_only_the_owner_changes_a_mod(self):
        self.assertIsNone(catalog.may_change("ann", None, "ann"), "a new mod by its owner")
        self.assertIsNotNone(catalog.may_change("ann", None, "bob"), "claiming someone else")
        self.assertIsNone(catalog.may_change("Ann", "ann", "ann"), "names compare without case")
        self.assertIsNotNone(catalog.may_change("bob", "ann", "bob"), "taking over a mod")
        self.assertIsNone(catalog.may_change("JoppieToppie", "ann", "bob"), "a maintainer")

    def test_submissions(self):
        s = catalog.parse_submission("x", {"download": "https://example.com/x.zip", "owner": "ann"})
        self.assertEqual("https://example.com/x.zip", s["download"])
        for bad in [{"download": "http://example.com/x.zip", "owner": "ann"},
                    {"download": "https://example.com/x.zip"},
                    {"download": "https://example.com/x.zip", "owner": "ann", "views": 99}]:
            with self.assertRaises(catalog.ModError):
                catalog.parse_submission("x", bad)

    def test_tags_and_links(self):
        self.assertEqual("my-mod-v1.2_beta", catalog.tag_of("my-mod", "1.2 beta"))
        self.assertTrue(catalog.asset_url("t", "x.zip").endswith("/releases/download/t/x.zip"))

    def test_images_are_16_9_jpegs(self):
        from PIL import Image
        for source in (png(100, 100), None):
            preview, thumb = catalog.images(source, "Name")
            self.assertEqual((1280, 720), Image.open(io.BytesIO(preview)).size)
            self.assertEqual((320, 180), Image.open(io.BytesIO(thumb)).size)

    def test_loose_json_keeps_strings(self):
        self.assertEqual({"a": "http://x//y", "b": [1]}, catalog.loose_json('{"a": "http://x//y", /* c */ "b": [1,],}'))


if __name__ == "__main__":
    unittest.main()
