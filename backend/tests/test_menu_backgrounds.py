from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from visual_retrieval import VisualCatalog
import server


class MenuBackgroundTests(unittest.TestCase):
    def test_menu_only_lists_existing_local_time_graded_artwork(self):
        catalog = VisualCatalog.__new__(VisualCatalog)
        catalog.backgrounds = [
            {'id': 'forest_path', 'file': 'forest_path__day.webp', 'time_mode': 'outdoor'},
            {'id': 'bedroom', 'file': 'bedroom__day.webp', 'time_mode': 'windowed'},
            {'id': 'mine', 'file': 'mine__day.webp', 'time_mode': 'fixed'},
            {'id': 'missing', 'file': 'missing__day.webp', 'time_mode': 'outdoor'},
            {'id': 'remote', 'file': 'https://example.org/remote__day.webp', 'time_mode': 'outdoor'},
        ]
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for name in ('forest_path', 'bedroom', 'mine'):
                (directory / f'{name}__day.webp').write_bytes(b'fixture')
            with patch('background_assets.background_directory', return_value=directory):
                items = catalog.menu_backgrounds()
        self.assertEqual([item['asset_id'] for item in items], ['forest_path', 'bedroom'])
        self.assertEqual(items[1]['lighting_profile'], 'windowed')
        self.assertEqual(items[0]['url'], '/backgrounds/forest_path__day.webp')

    def test_menu_catalog_needs_no_key_and_never_calls_deepseek(self):
        with patch.object(server, '_client', side_effect=AssertionError('No paid calls')), TestClient(server.app) as api:
            response = api.get('/visual/menu-backgrounds')
            self.assertEqual(response.status_code, 200)
            items = response.json()['backgrounds']
            self.assertGreater(len(items), 1)
            self.assertTrue(all(item['time_mode'] in {'outdoor', 'windowed'} for item in items))
            self.assertTrue(all('prompt' not in item and 'time_of_day' not in item for item in items))
            self.assertEqual(api.get('/visual/menu-backgrounds', headers={'Origin': 'https://example.org'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
