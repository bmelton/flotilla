"""Pure-function tests. Run: python3 -m unittest discover tests"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import flotilla as f  # noqa: E402


class Urls(unittest.TestCase):
    def test_default_url(self):
        self.assertEqual(f.default_url("sv08.local", "moonraker"), "http://sv08.local/webcam/?action=stream")
        self.assertEqual(f.default_url("https://q2.lan:7125/", ""), "https://q2.lan:7125/webcam/?action=stream")
        self.assertEqual(f.default_url("cc.lan", "ElegooLink"), "http://cc.lan:3031/video")
        self.assertEqual(f.default_url("", "moonraker"), "")

    def test_candidate_urls_fallback(self):
        urls = f.candidate_urls("u1.lan", "moonraker")
        self.assertEqual(urls[0], "http://u1.lan/webcam/?action=stream")
        self.assertIn("http://u1.lan/webcam/stream.mjpg", urls)  # Snapmaker U1 mod (#5)
        self.assertEqual(f.candidate_urls("cc.lan", "elegoolink"), ["http://cc.lan:3031/video"])
        self.assertTrue(f.is_elegoo_stream(f.default_url("cc.lan", "ElegooLink")))
        self.assertFalse(f.is_elegoo_stream("http://u1.lan/webcam/stream.mjpg") or f.is_elegoo_stream(None))
        t = f.tiles([{"name": "U1", "print_host": "u1.lan", "host_type": "moonraker"},
                     {"name": "Q2", "print_host": "b", "host_type": ""}], {"Q2": "http://relay/x"})
        self.assertEqual(t[0]["alts"], urls[1:])
        self.assertEqual(t[1]["alts"], [])  # explicit config: no guessing
        page = f.page_html(t)
        self.assertIn("stream.mjpg", page)
        self.assertIn("REFRESH_MS", page)  # #6: <img> live tiles re-request every 15 min
        self.assertIn("createImageBitmap", page)  # #9: canvas path, no multipart <img> port leak
        self.assertNotIn("<img src=", page)  # JS decides when a tile opens its stream

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_parses(self):
        p = pathlib.Path(tempfile.mkdtemp()) / "page.js"
        p.write_text("const orca={postMessage(){}};\n" + f._JS)
        subprocess.run(["node", "--check", str(p)], check=True)


class Printers(unittest.TestCase):
    def test_dedupe_and_tiles(self):
        d = f.dedupe([{"name": "SV08 - Copy", "print_host": "a", "host_type": ""}, {"name": "SV08", "print_host": "a", "host_type": ""},
                      {"name": "sys", "print_host": "", "host_type": ""}, {"name": "Q2", "print_host": "b", "host_type": ""}])
        self.assertEqual([p["name"] for p in d], ["Q2", "SV08"])
        t = f.tiles([{"name": "Q2", "print_host": "b", "host_type": ""}], {"Q2": "http://relay/x"})
        self.assertEqual(t[0]["url"], "http://relay/x")
        page = f.page_html(t)
        for needle in ("Q2", "data-host='b'", "⚠ E-Stop", "class='row confirm'", "id='ar'"):
            self.assertIn(needle, page)
        self.assertIn("Q2", f.config_html(d))


class Status(unittest.TestCase):
    def test_moonraker(self):
        sm = f.summarize({"status": {"print_stats": {"state": "printing", "filename": "a.gcode", "print_duration": 600},
                                     "display_status": {"progress": 0.25}, "extruder": {"temperature": 210.4, "target": 210},
                                     "heater_bed": {"temperature": 59.6, "target": 60}}})
        self.assertEqual((sm["progress"], sm["remaining"], sm["nozzle"]), (25, 1800, [210, 210]))
        self.assertIsNone(f.summarize({"status": {}})["remaining"])

    def test_sdcp(self):
        ss = f.summarize_sdcp({"PrintInfo": {"Status": 13, "CurrentTicks": 2615.4, "TotalTicks": 10314, "Filename": "x.gcode", "Progress": 20},
                               "TempOfNozzle": 234.85, "TempTargetNozzle": 235, "TempOfHotbed": 80.1, "TempTargetHotbed": 80})
        self.assertEqual((ss["state"], ss["remaining"], ss["nozzle"]), ("printing", 7698, [235, 235]))
        self.assertEqual(f.summarize_sdcp({"PrintInfo": {"Status": 6}})["state"], "paused")


class Log(unittest.TestCase):
    def test_jsonl_with_printer_context(self):
        d = pathlib.Path(tempfile.mkdtemp())
        old = f.log_path
        f.log_path = lambda: d / "flotilla.log"
        try:
            f._names["h"] = "P"; f._firmware["h"] = "fw1"
            f.log("status", "h", cmd="status", state="printing"); f.log("plugin_loaded", python="3")
            lines = [json.loads(l) for l in f.log_path().read_text().splitlines()]
        finally:
            f.log_path = old
        self.assertEqual((lines[0]["printer"], lines[0]["firmware"], lines[1]["event"]), ("P", "fw1", "plugin_loaded"))


if __name__ == "__main__":
    unittest.main()
