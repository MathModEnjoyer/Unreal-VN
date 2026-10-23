import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from visual_retrieval import VisualCatalog, _lighting_for
from import_backgrounds import import_pack
from PIL import Image
from background_lighting import fallback_definition


class BackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        with patch("pathlib.Path.home", return_value=Path(cls.home.name)):
            import server
        cls.server = server
        cls.client = TestClient(server.app)
        cls.catalog = VisualCatalog()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.home.cleanup()

    def test_same_day_source_for_every_time(self):
        matches = [self.catalog.recommend(f"Спальня {when}", background_id="bedroom")["backgrounds"][0]
                   for when in ("день", "утро", "вечер", "ночь")]
        self.assertEqual({m["asset_id"] for m in matches}, {"bedroom"})
        self.assertEqual({m["file"] for m in matches}, {"bedroom__day.webp"})
        self.assertEqual([m["time_of_day"] for m in matches], ["day", "morning", "evening", "night"])
        self.assertEqual({m["lighting_profile"] for m in matches}, {"windowed"})

    def test_current_scene_lighting_ignores_previous_night(self):
        state = SimpleNamespace(genre="драма", tone="лёгкий", facts=["Ночью он встретил друга"], recent_lines=[])
        result = self.server._scene_with_visuals(state, {"location": "Спальня", "location_background_id": "bedroom", "narration": "Утром стало тепло."})
        self.assertEqual(result["visual"]["backgrounds"][0]["time_of_day"], "morning")

    def test_pack_files_and_http(self):
        root = Path(__file__).resolve().parents[1]
        self.assertGreater(len(self.catalog.backgrounds), 0)
        for item in self.catalog.backgrounds:
            file = item["file"]
            self.assertTrue(file.endswith("__day.webp"))
            response = self.client.get(f"/backgrounds/{file}")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("image/webp"))
            self.assertEqual(hashlib.sha256(response.content).hexdigest(), item["sha256"])
            with Image.open(root / "backgrounds" / file) as image:
                self.assertLessEqual(image.width, 1280)
        self.assertEqual(self.client.get("/backgrounds/missing__day.webp").status_code, 404)
        self.assertEqual(self.client.get("/backgrounds/..%2fserver.py").status_code, 404)

    def test_recommend_api_and_fallback(self):
        response = self.client.post("/visual/recommend", json={"text": "Спальня, вечер", "background_id": "bedroom"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["backgrounds"][0]["time_of_day"], "evening")
        for query in ("", "qwertyzzzx"):
            match = self.catalog.recommend(query)["backgrounds"][0]
            self.assertEqual(match["asset_id"], "abstract_space")
            self.assertTrue(match["is_fallback"])
            self.assertEqual(match["time_mode"], "fixed")
            if match["url"]:
                self.assertEqual(self.client.get(match["url"]).status_code, 200)
        self.assertEqual(_lighting_for("Ночной лес"), "night")
        self.assertEqual(_lighting_for("Сумеречный лес"), "evening")

    def test_fixed_locations_ignore_time_and_do_not_change_source(self):
        for query, asset_id in (("Шахта", "mine"), ("Серверная", "server_room"), ("Вагон метро", "metro_car"), ("Мостик космического корабля", "spaceship_bridge")):
            for when in ("утро", "день", "вечер", "ночь"):
                match = self.catalog.recommend(query, lighting_query=when, background_id=asset_id)["backgrounds"][0]
                self.assertEqual(match["asset_id"], asset_id)
                self.assertEqual(match["time_of_day"], "neutral")
                self.assertEqual(match["time_mode"], "fixed")
                self.assertEqual(match["file"], asset_id + "__day.webp")

    def test_unknown_current_location_does_not_reuse_old_bedroom(self):
        state = SimpleNamespace(genre="драма", tone="лёгкий", facts=["В спальне стояла кровать"], recent_lines=[])
        result = self.server._scene_with_visuals(state, {"location": "qwertyzzzx", "narration": "Ночью они прибыли."})
        match = result["visual"]["backgrounds"][0]
        self.assertEqual(match["asset_id"], "abstract_space")
        self.assertEqual(match["time_of_day"], "neutral")

    def test_fallback_is_last_resort_even_with_no_compatible_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fallback = {**fallback_definition(), "file": "abstract_space__day.webp"}
            catalog = {"places": {"restricted": {"label": "Спальня", "file": "restricted__day.webp", "worlds": ["one"]}}, "fallback": fallback}
            (root / "background_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            lookup = VisualCatalog(root)
            for world in ("two", None):
                match = lookup.recommend("qwertyzzzx ночь", world=world)["backgrounds"][0]
                self.assertTrue(match["is_fallback"])
                self.assertEqual(match["url"], "/backgrounds/abstract_space__day.webp")
                self.assertEqual(match["time_of_day"], "neutral")
            self.assertNotIn("abstract_space", [match.asset_id for match in lookup.rank_backgrounds("qwertyzzzx")])

    def test_import_preserves_lighting_modes_and_separate_optional_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "run"
            (source / "originals").mkdir(parents=True)
            locations = [{"id": "mine", "lighting": "enclosed"}, {"id": "metro_car", "lighting": "windowed"}, {"id": "bedroom", "lighting": "windowed"}, fallback_definition()]
            (source / "locations.json").write_text(json.dumps(locations), encoding="utf-8")
            for key in ("mine", "metro_car", "bedroom"):
                Image.new("RGB", (1280, 720), "green").save(source / f"originals/{key}__day.png")
            report = import_pack(source, root / "game")
            self.assertEqual(report["planned"], 3)
            self.assertEqual(report["missing"], [])
            self.assertFalse(report["fallback_ready"])
            Image.new("RGB", (1280, 720), "gray").save(source / "originals/abstract_space__day.png")
            report = import_pack(source, root / "game")
            self.assertTrue(report["fallback_ready"])
            catalog = json.loads((root / "game/visual_catalogs/background_catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["places"]["mine"]["time_mode"], "fixed")
            self.assertEqual(catalog["places"]["metro_car"]["time_mode"], "fixed")
            self.assertEqual(catalog["places"]["bedroom"]["time_mode"], "windowed")
            self.assertNotIn("abstract_space", catalog["places"])
            self.assertEqual(catalog["fallback"]["time_mode"], "fixed")

    def test_import_never_uses_old_morning_as_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "run"
            (source / "originals").mkdir(parents=True)
            (source / "backgrounds/realesrgan_x2").mkdir(parents=True)
            locations = [{"id": key, "label": key, "lighting": "outdoor"} for key in ("one", "two", "missing")]
            (source / "locations.json").write_text(json.dumps(locations), encoding="utf-8")
            Image.new("RGB", (1280, 720), "green").save(source / "originals/one__day.png")
            Image.new("RGB", (1920, 1080), "blue").save(source / "backgrounds/realesrgan_x2/two__day.png")
            Image.new("RGB", (1280, 720), "red").save(source / "originals/missing__morning.png")
            report = import_pack(source, root / "game")
            self.assertEqual(report["imported"], 2)
            self.assertEqual(report["missing"], ["missing"])
            self.assertEqual(len(list((root / "game/backgrounds").iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
