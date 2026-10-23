import asyncio
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from music_assets import MusicAssets, MusicUnavailable, valid_source


class MusicAssetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = b"ID3" + bytes(range(100))
        self.track = SimpleNamespace(download_url="https://ccmixter.org/content/example/Example.mp3",
                                     sha256=hashlib.sha256(self.data).hexdigest(), byte_size=len(self.data))
        self.requests = []

    def assets(self, handler):
        async def record(request):
            self.requests.append(request)
            return await handler(request)
        return MusicAssets(self.root, client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(record), **kwargs))

    async def good(self, request):
        return httpx.Response(200, content=self.data)

    async def test_download_once_and_reuse_offline_after_restart(self):
        store = self.assets(self.good)
        path = await store.ensure(self.track)
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(len(self.requests), 1)

        async def offline(request):
            raise httpx.ConnectError("offline")

        restarted = self.assets(offline)
        self.assertEqual(await restarted.ensure(self.track), path)
        self.assertEqual(len(self.requests), 1)

    async def test_concurrent_requests_share_one_download(self):
        async def slow(request):
            await asyncio.sleep(.01)
            return httpx.Response(200, content=self.data)
        store = self.assets(slow)
        paths = await asyncio.gather(*(store.ensure(self.track) for _ in range(4)))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(len(self.requests), 1)

    async def test_wrong_hash_or_oversized_response_is_not_cached_and_can_retry(self):
        for data in [b"x" * len(self.data), self.data + b"extra"]:
            async def wrong(request):
                return httpx.Response(200, content=data)
            with self.assertRaises(MusicUnavailable):
                await self.assets(wrong).ensure(self.track)
            self.assertFalse(list(self.root.iterdir()))
        self.assertEqual((await self.assets(self.good).ensure(self.track)).read_bytes(), self.data)

    async def test_corrupt_existing_cache_is_replaced_not_served(self):
        target = self.root / f"{self.track.sha256}.mp3"
        target.write_bytes(b"x" * len(self.data))
        self.assertEqual(await self.assets(self.good).ensure(self.track), target)
        self.assertEqual(target.read_bytes(), self.data)
        self.assertEqual(len(self.requests), 1)

    async def test_network_failure_cleans_partial_download(self):
        async def unavailable(request):
            raise httpx.ConnectError("offline")
        with self.assertRaises(MusicUnavailable):
            await self.assets(unavailable).ensure(self.track)
        self.assertFalse(list(self.root.iterdir()))

    async def test_download_identifies_game_and_source_for_ccmixter(self):
        await self.assets(self.good).ensure(self.track)
        self.assertEqual(self.requests[0].headers["Referer"], "https://ccmixter.org/")
        self.assertIn("StoryEngine", self.requests[0].headers["User-Agent"])

    async def test_untrusted_sources_and_redirects_are_rejected(self):
        for url in ["http://ccmixter.org/content/a.mp3",
                    "https://localhost/private.mp3", "https://ccmixter.org@evil.test/a.mp3",
                    "https://ccmixter.org/content/%2Fetc.mp3"]:
            self.assertFalse(valid_source(url, self.track.sha256, self.track.byte_size))
        async def redirect(request):
            return httpx.Response(302, headers={"Location": "https://localhost/private.mp3"})
        with self.assertRaises(MusicUnavailable):
            await self.assets(redirect).ensure(self.track)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(list(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
