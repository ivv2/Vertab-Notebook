"""Tests for vertab_core -- the parts that decide whether notes survive.

    python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vertab_core as core  # noqa: E402


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name
        self.storage = core.Storage(self.folder)
        self.notes_path = os.path.join(self.folder, core.NOTES_FILE)

    def tearDown(self):
        self._tmp.cleanup()

    def write_raw(self, text, name=core.NOTES_FILE):
        with open(os.path.join(self.folder, name), "w", encoding="utf-8") as f:
            f.write(text)


class NotesTests(StorageTestCase):
    def test_round_trip_keeps_unicode(self):
        notes = {"latvian": "Darbs maxim\u0101 nav jauks", "art": " /\\_/\\\n( o.o )"}
        self.storage.save_notes(notes)
        loaded, warning = self.storage.load_notes()
        self.assertEqual(loaded, notes)
        self.assertIsNone(warning)

    def test_missing_file_is_an_empty_notebook(self):
        self.assertEqual(self.storage.load_notes(), ({}, None))

    def test_damaged_file_is_moved_aside_not_overwritten(self):
        self.write_raw('{"math": "x = 1",')  # truncated mid-write
        notes, warning = self.storage.load_notes()
        self.assertEqual(notes, {})
        self.assertIn("could not be read", warning)
        damaged = [n for n in os.listdir(self.folder) if ".damaged-" in n]
        self.assertEqual(len(damaged), 1)
        with open(os.path.join(self.folder, damaged[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"math": "x = 1",')
        self.assertFalse(os.path.exists(self.notes_path))

    def test_non_object_json_is_treated_as_damaged(self):
        self.write_raw('["not", "a", "notebook"]')
        notes, warning = self.storage.load_notes()
        self.assertEqual(notes, {})
        self.assertIsNotNone(warning)

    def test_saving_is_refused_when_damaged_file_cannot_be_moved(self):
        self.write_raw("{broken")
        with mock.patch.object(core, "replace_with_retry", side_effect=PermissionError("locked")):
            _notes, warning = self.storage.load_notes()
        self.assertIn("saving is disabled", warning)
        with self.assertRaises(core.NotebookError):
            self.storage.save_notes({"new": "note"})
        with open(self.notes_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{broken")

    def test_non_string_values_are_coerced(self):
        self.write_raw(json.dumps({"n": 5, "list": [1, 2]}))
        notes, _ = self.storage.load_notes()
        self.assertEqual(notes, {"n": "5", "list": "[1, 2]"})

    def test_first_save_of_a_session_keeps_a_backup(self):
        self.write_raw(json.dumps({"old": "version"}))
        self.storage.save_notes({"new": "version"})
        self.storage.save_notes({"newer": "version"})
        with open(self.notes_path + ".bak", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"old": "version"})

    def test_failed_write_leaves_previous_file_intact(self):
        self.storage.save_notes({"keep": "me"})
        with mock.patch.object(core.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(core.NotebookError):
                self.storage.save_notes({"lost": "write"})
        loaded, _ = self.storage.load_notes()
        self.assertEqual(loaded, {"keep": "me"})
        leftovers = [n for n in os.listdir(self.folder) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_prepare_adopts_legacy_notes_once(self):
        legacy = tempfile.mkdtemp()
        try:
            with open(os.path.join(legacy, core.NOTES_FILE), "w", encoding="utf-8") as f:
                json.dump({"legacy": "note"}, f)
            target = os.path.join(self.folder, "sub")
            storage = core.Storage(target)
            storage.prepare(legacy_dir=legacy)
            self.assertEqual(storage.load_notes()[0], {"legacy": "note"})
            storage.save_notes({"edited": "note"})
            storage.prepare(legacy_dir=legacy)  # must not clobber newer notes
            self.assertEqual(storage.load_notes()[0], {"edited": "note"})
        finally:
            import shutil
            shutil.rmtree(legacy, ignore_errors=True)


class ConfigTests(StorageTestCase):
    def test_defaults_when_missing(self):
        self.assertEqual(self.storage.load_config(), core.DEFAULT_CONFIG)

    def test_garbage_file_falls_back_to_defaults(self):
        self.write_raw("not json at all", core.CONFIG_FILE)
        self.assertEqual(self.storage.load_config(), core.DEFAULT_CONFIG)

    def test_wrong_types_are_dropped_per_key(self):
        self.write_raw(json.dumps({
            "opacity": "0.5", "topmost": 1, "theme": "darkly",
            "overlay_mode": True, "unknown": "ignored",
        }), core.CONFIG_FILE)
        config = self.storage.load_config()
        self.assertEqual(config["opacity"], core.DEFAULT_CONFIG["opacity"])
        self.assertEqual(config["topmost"], core.DEFAULT_CONFIG["topmost"])
        self.assertEqual(config["theme"], "darkly")
        self.assertTrue(config["overlay_mode"])
        self.assertNotIn("unknown", config)

    def test_opacity_is_clamped(self):
        self.assertEqual(core.validate_config({"opacity": 0})["opacity"], core.MIN_OPACITY)
        self.assertEqual(core.validate_config({"opacity": 7})["opacity"], core.MAX_OPACITY)

    def test_malformed_geometry_and_theme_are_rejected(self):
        config = core.validate_config({
            "window_geometry": "700x500; destroy .",
            "overlay_geometry": "380x460+-8+48",
            "theme": "../../evil",
        })
        self.assertEqual(config["window_geometry"], "")
        self.assertEqual(config["overlay_geometry"], "380x460+-8+48")
        self.assertEqual(config["theme"], core.DEFAULT_CONFIG["theme"])

    def test_command_line_overrides_saved_mode(self):
        self.storage.save_config(dict(core.DEFAULT_CONFIG, overlay_mode=True))
        self.assertFalse(self.storage.load_config(argv=["--window"])["overlay_mode"])
        self.assertTrue(self.storage.load_config(argv=["--overlay"])["overlay_mode"])


class GeometryTests(unittest.TestCase):
    SCREEN = (0, 0, 1920, 1040)  # work area above a 40px taskbar

    def test_on_screen_geometry_is_untouched(self):
        self.assertEqual(core.clamp_geometry("380x460+100+48", self.SCREEN), "380x460+100+48")

    def test_off_screen_window_is_pulled_back(self):
        self.assertEqual(core.clamp_geometry("380x460+3500+48", self.SCREEN), "380x460+1540+48")
        self.assertEqual(core.clamp_geometry("380x460+-900+-50", self.SCREEN), "380x460+0+0")

    def test_oversized_window_shrinks_to_fit(self):
        self.assertEqual(core.clamp_geometry("4000x3000+0+0", self.SCREEN), "1920x1040+0+0")

    def test_unpositioned_geometry_passes_through(self):
        self.assertEqual(core.clamp_geometry("700x500", self.SCREEN), "700x500")
        self.assertEqual(core.clamp_geometry("", self.SCREEN), "")

    def test_snap_to_nearby_edges_only(self):
        self.assertEqual(core.snap_position(10, 1030 - 460, 380, 460, self.SCREEN, 18),
                         (0, 1040 - 460))
        self.assertEqual(core.snap_position(500, 300, 380, 460, self.SCREEN, 18), (500, 300))
        self.assertEqual(core.snap_position(1920 - 380 - 12, 5, 380, 460, self.SCREEN, 18),
                         (1920 - 380, 0))

    def test_snap_respects_offset_monitor(self):
        second = (1920, 0, 3840, 1080)
        self.assertEqual(core.snap_position(1930, 500, 380, 460, second, 18), (1920, 500))


class InstanceNameTests(unittest.TestCase):
    def test_stable_and_folder_specific(self):
        a = core.instance_name(r"C:\Users\x\AppData\Roaming\VerTab")
        self.assertEqual(a, core.instance_name(r"C:\Users\x\AppData\Roaming\VerTab"))
        self.assertNotEqual(a, core.instance_name(r"D:\portable\VerTab"))
        self.assertTrue(a.startswith("Local\\VerTab-"))


if __name__ == "__main__":
    unittest.main()
