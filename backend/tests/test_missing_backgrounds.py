import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from missing_backgrounds import MissingBackgroundJournal, markdown_report
from visual_retrieval import VisualCatalog


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "journal.sqlite3"
        self.journal = MissingBackgroundJournal(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def record(self, node_id="one", session_id="story", **kwargs):
        scene = kwargs.pop("scene", {"location": "Колесо обозрения", "narration": "Внизу светился парк.", "time_of_day": "ночь"})
        return self.journal.record(session_id=session_id, node_id=node_id, scene=scene,
                                   genre=kwargs.pop("genre", "драма"), **kwargs)

    def test_persistence_dedup_and_distinct_sessions(self):
        self.assertTrue(self.record())
        self.assertFalse(self.record())
        self.assertFalse(MissingBackgroundJournal(self.path).record(
            session_id="story", node_id="one", scene={"location": "Изменённый текст"}))
        self.record(node_id="two")
        self.record(session_id="another")
        report = MissingBackgroundJournal(self.path).report()
        self.assertEqual(report["location_count"], 1)
        self.assertEqual(report["scene_count"], 3)
        self.assertEqual(len(report["locations"][0]["scenes"]), 3)

    def test_grouping_keeps_genres_and_unnamed_places_separate(self):
        self.record(scene={"location": "  Колёсо  обозрения "})
        self.record(node_id="two", scene={"location": "колесо обозрения"})
        self.record(node_id="three", genre="фэнтези")
        self.record(node_id="four", scene={"narration": "Внутри планетария."})
        self.record(node_id="five", scene={"narration": "Внутри капсулы."})
        report = self.journal.report()
        self.assertEqual(report["location_count"], 4)
        self.assertEqual(report["locations"][0]["scene_count"], 2)

    def test_identity_without_tree_node_and_concurrent_reload(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.record(node_id=None), range(12)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.journal.report()["scene_count"], 1)
        self.assertFalse(self.record(session_id=""))

    def test_empty_read_does_not_create_file_and_export_has_context(self):
        self.assertEqual(self.journal.report()["scene_count"], 0)
        self.assertFalse(self.path.exists())
        self.record(reason="low_confidence", requested_time="night",
                    nearest_candidates=[{"asset_id": "camp", "score": 0.1}])
        report = self.journal.report()
        restored = json.loads(json.dumps(report, ensure_ascii=False))
        scene = restored["locations"][0]["scenes"][0]
        self.assertEqual(scene["requested_time_of_day"], "night")
        self.assertEqual(scene["nearest_candidates"][0]["asset_id"], "camp")
        text = markdown_report(restored)
        self.assertIn("Колесо обозрения", text)
        self.assertIn("Внизу светился парк", text)
        self.assertIn("слабое совпадение", text)

    def test_selection_reasons_do_not_guess_from_prose(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = VisualCatalog(directory)
            self.assertEqual(catalog.recommend('Спальня')['fallback']['reason'], 'no_selection')
            result = catalog.recommend('место', lighting_query='утро', background_id='made_up')
            self.assertEqual(result['fallback']['reason'], 'invalid_background_id')
            self.assertEqual(result['fallback']['nearest_candidates'], [])
            self.assertEqual(result['requested_time_of_day'], 'morning')


class JournalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        with patch("pathlib.Path.home", return_value=Path(cls.home.name)):
            import server
        cls.server = server
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.home.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "journal.sqlite3"
        self.environment = patch.dict(os.environ, {"STORY_ENGINE_MISSING_BACKGROUNDS_PATH": str(self.path)})
        self.environment.start()
        self.state = SimpleNamespace(genre="драма", tone="лёгкий", facts=["PRIVATE_OLD_CONTEXT"],
                                     recent_lines=[], tree_current_id="node", delivered_scenes=3)
        # Результат не зависит от наличия реального каталога в тестовой папке.
        self.catalog_patch = patch.object(self.server, "get_visual_catalog", return_value=VisualCatalog(Path(self.directory.name)))
        self.catalog_patch.start()

    def tearDown(self):
        self.catalog_patch.stop()
        self.environment.stop()
        self.directory.cleanup()

    def test_display_hook_dedup_exports_and_diagnostic_excluded(self):
        response = self.client.post("/visual/recommend", json={"text": "неизвестно"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.path.exists())
        scene = {"location": "Колесо обозрения", "narration": "Утром виден парк."}
        for _ in range(2):
            result = self.server._scene_with_visuals(self.state, scene, session_id="test")
        self.assertTrue(result["visual"]["backgrounds"][0]["is_fallback"])
        self.assertNotIn("visual", scene)
        response = self.client.get("/visual/missing-backgrounds")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scene_count"], 1)
        self.assertEqual(response.json()["locations"][0]["times_of_day"], {"morning": 1})
        self.assertNotIn("PRIVATE_OLD_CONTEXT", response.text)
        exported = self.client.get("/visual/missing-backgrounds/export?format=json")
        self.assertEqual(exported.json(), response.json())
        self.assertIn("attachment", exported.headers["content-disposition"])
        self.assertIn("Колесо обозрения", self.client.get("/visual/missing-backgrounds/export").text)
        self.assertEqual(self.client.get("/visual/missing-backgrounds/export?format=bad").status_code, 422)

    def test_real_background_is_not_recorded(self):
        catalog = self.server.get_visual_catalog()
        with patch.object(catalog, "recommend", return_value={"backgrounds": [{"asset_id": "mine", "is_fallback": False}]}):
            self.server._scene_with_visuals(self.state, {"location": "Шахта"}, session_id="test")
        self.assertFalse(self.path.exists())

    def test_catalog_error_is_distinct_from_missing_location(self):
        with patch.object(self.server, "get_visual_catalog", side_effect=RuntimeError("unavailable")):
            result = self.server._scene_with_visuals(self.state, {"location": "Парк"}, session_id="test")
        self.assertEqual(result["visual"]["error"], "catalog_unavailable")
        report = MissingBackgroundJournal(self.path).report()
        self.assertEqual(report["locations"][0]["reasons"], {"catalog_unavailable": 1})
        self.assertIn("проверить ошибку до генерации", markdown_report(report))

    def test_failed_write_does_not_break_story_and_failed_read_is_503(self):
        with patch.object(self.server.MissingBackgroundJournal, "record", side_effect=OSError("disk full")):
            with self.assertLogs("server", level="ERROR"):
                result = self.server._scene_with_visuals(self.state, {"location": "Парк"}, session_id="test")
        self.assertTrue(result["visual"]["missing_background_log_error"])
        self.assertIn("music", result)
        self.path.write_bytes(b"broken database")
        with self.assertLogs("server", level="ERROR"):
            self.assertEqual(self.client.get("/visual/missing-backgrounds").status_code, 503)


if __name__ == "__main__":
    unittest.main()
