import html
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from music_retrieval import MusicCatalog, music_query
from music_assets import valid_source, MusicUnavailable

ROOT = Path(__file__).resolve().parents[1] / "music_catalogs"


class MusicLibraryTests(unittest.TestCase):
    def test_remote_files_have_credits_and_verified_provenance_without_bundled_audio(self):
        catalog = MusicCatalog()
        self.assertGreaterEqual(len(catalog.tracks), 12)
        manifest = json.loads((ROOT / "provenance.json").read_text(encoding="utf-8"))
        evidence = {item["id"]: item for item in manifest["tracks"]}
        credits = (ROOT / "CREDITS.html").read_text(encoding="utf-8")
        for track in catalog.tracks:
            with self.subTest(track=track.id):
                self.assertIsNone(track.file)
                self.assertTrue(valid_source(track.download_url, track.sha256, track.byte_size))
                self.assertEqual(track.sha256, evidence[track.id]["sha256"])
                self.assertEqual(track.byte_size, evidence[track.id]["bytes"])
                self.assertEqual(track.license, "CC-BY-3.0")
                self.assertTrue(track.artist)
                self.assertIn(track.artist, track.attribution)
                self.assertIn(html.escape(track.title), credits)
                self.assertIn(html.escape(track.artist), credits)
                self.assertEqual(track.license_url, "https://creativecommons.org/licenses/by/3.0/")
                for sample in evidence[track.id]["sample_sources"]:
                    self.assertIn(html.escape(sample["source"]), credits)
                    self.assertEqual(sample["license_url"], track.license_url)
                public = track.as_dict()
                self.assertEqual(public["artist"], track.artist)
                self.assertNotIn("download_url", public)
                self.assertRegex(track.url, r"^/music/[a-z0-9_-]+\.mp3$")
        self.assertFalse(list(ROOT.rglob("*.ogg")) + list(ROOT.rglob("*.mp3")))

    def test_genres_and_current_moods_have_playable_choices(self):
        catalog = MusicCatalog()
        genres = ["детектив", "фэнтези", "sci-fi", "хоррор", "драма", "приключение", "мистика", "производственный роман"]
        for genre in genres:
            with self.subTest(genre=genre):
                self.assertFalse(catalog.recommend("Разговор в комнате", genre=genre)["silence"])
        for text, mood in [("Началась погоня", "action"), ("Появилось чудовище", "horror"),
                           ("В комнате опасно", "tense"), ("Она плачет от утраты", "sad"),
                           ("Нежный поцелуй", "romantic"), ("Нашли улику", "mystery"),
                           ("Все радостно празднуют победу", "joyful"), ("Весёлый разговор", "joyful"),
                           ("Спокойный отдых", "calm")]:
            result = catalog.recommend(text)
            with self.subTest(text=text):
                self.assertEqual(result["query"]["preferred_mood"], mood)
                self.assertTrue(result["selected"])
                self.assertIn(mood, result["tracks"][0]["moods"])
        self.assertEqual(catalog.recommend("Тихий разговор без опасности")["query"]["preferred_mood"], "calm")

    def test_missing_disallowed_and_escaping_files_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "good.ogg").write_bytes(b"OggS")
            records = [dict(id="good", file="good.ogg", license="CC0", moods=["calm"]),
                       dict(id="missing", file="missing.ogg", license="CC0"),
                       dict(id="restricted", file="good.ogg", license="CC-BY-NC"),
                       dict(id="escape", file="../outside.ogg", license="CC0")]
            path = root / "catalog.json"
            path.write_text(json.dumps({"tracks": records}), encoding="utf-8")
            self.assertEqual([x.id for x in MusicCatalog(path).tracks], ["good"])

    def test_frozen_application_uses_bundled_catalog(self):
        with patch.object(sys, "_MEIPASS", str(ROOT.parent), create=True):
            self.assertEqual(MusicCatalog().path, ROOT / "music_catalog.json")

    def test_scene_music_does_not_use_historical_visual_query(self):
        import server
        scene = {"location": "Дом", "narration": "Спокойный разговор за чаем"}
        state = SimpleNamespace(genre="драма", tone="лёгкий", recent_lines=["Бой"], event_history=[])
        with patch.object(server, "visual_query", return_value="Погоня, бой и выстрелы"), \
             patch.object(server, "get_visual_catalog") as visuals:
            visuals.return_value.recommend.return_value = {"backgrounds": []}
            result = server._scene_with_visuals(state, scene)
        self.assertEqual(result["music"]["query"]["preferred_mood"], "calm")
        self.assertNotIn("Бой", music_query(scene))
        self.assertNotIn("music", scene)


class MusicRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import server
        cls.client = TestClient(server.app)

    def test_audio_is_served_locally_with_audio_type_and_range_support(self):
        import server
        track = MusicCatalog().tracks[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cached.mp3"
            path.write_bytes(b"ID3" + bytes(range(100)))
            assets = SimpleNamespace(ensure=AsyncMock(return_value=path))
            with patch.object(server, "get_music_assets", return_value=assets):
                response = self.client.get(track.url, headers={"Range": "bytes=0-63"})
        self.assertEqual(response.status_code, 206)
        self.assertIn("audio/mpeg", response.headers["content-type"])
        self.assertEqual(len(response.content), 64)
        self.assertTrue(response.content.startswith(b"ID3"))
        self.assertIn("immutable", response.headers["cache-control"])

    def test_unavailable_audio_does_not_break_game_or_download_unknown_urls(self):
        import server
        assets = SimpleNamespace(ensure=AsyncMock(side_effect=MusicUnavailable("offline")))
        with patch.object(server, "get_music_assets", return_value=assets):
            self.assertEqual(self.client.get("/music/unknown.mp3").status_code, 404)
            assets.ensure.assert_not_called()
            self.assertEqual(self.client.get(MusicCatalog().tracks[0].url).status_code, 503)
            self.assertEqual(self.client.get("/health").status_code, 200)

    def test_credits_and_missing_file_do_not_fall_through_to_spa(self):
        response = self.client.get("/music-credits")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("creativecommons.org/licenses/by/3.0/", response.text)
        self.assertIn("ccMixter", response.text)
        self.assertEqual(self.client.get("/music/nonexistent.ogg").status_code, 404)


if __name__ == "__main__":
    unittest.main()
