"""Загрузка выбранной композиции с официального источника и постоянный кеш."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

MAX_AUDIO_BYTES = 40 * 1024 * 1024
SOURCE_HOSTS = {"ccmixter.org", "www.ccmixter.org"}
SOURCE_PREFIX = "/content/"


class MusicUnavailable(Exception):
    pass


def valid_source(url: str, sha256: str, size: int) -> bool:
    try:
        parsed = urlsplit(url)
        name = unquote(parsed.path.removeprefix(SOURCE_PREFIX))
        return (parsed.scheme == "https" and parsed.netloc in SOURCE_HOSTS
                and parsed.path.startswith(SOURCE_PREFIX) and name.lower().endswith(".mp3")
                and bool(re.fullmatch(r"[a-zA-Z0-9_-]+/[^/\\\x00\r\n]+\.mp3", name, re.I))
                and bool(re.fullmatch(r"[0-9a-f]{64}", sha256))
                and isinstance(size, int) and 0 < size <= MAX_AUDIO_BYTES)
    except (TypeError, ValueError, AttributeError):
        return False


class MusicAssets:
    def __init__(self, cache_dir: Path | None = None, *, client_factory=httpx.AsyncClient):
        self.cache_dir = cache_dir or Path(os.getenv("STORY_ENGINE_MUSIC_CACHE") or
                                         Path.home() / ".story_engine/cache/music")
        self.client_factory = client_factory
        self._locks: dict[str, asyncio.Lock] = {}
        self._verified: dict[Path, tuple[int, int]] = {}

    def _publish(self, partial: Path, target: Path, sha256: str, size: int) -> Path:
        if partial.stat().st_size != size or hashlib.sha256(partial.read_bytes()).hexdigest() != sha256:
            raise MusicUnavailable("Music file failed integrity check")
        os.replace(partial, target)
        stat = target.stat()
        self._verified[target] = (stat.st_size, stat.st_mtime_ns)
        return target

    def _cached(self, path: Path, sha256: str, size: int) -> bool:
        try:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if stat.st_size != size:
                return False
            if self._verified.get(path) != signature:
                if hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
                    return False
                self._verified[path] = signature
            return True
        except OSError:
            return False

    async def ensure(self, track) -> Path:
        url, sha256, size = track.download_url, track.sha256, track.byte_size
        if not valid_source(url, sha256, size):
            raise MusicUnavailable("Invalid music source")
        target = self.cache_dir / f"{sha256}.mp3"
        # Only a request for a selected audio URL calls this method. Scene generation
        # and recommendations never download music or depend on this network request.
        async with self._locks.setdefault(sha256, asyncio.Lock()):
            if await asyncio.to_thread(self._cached, target, sha256, size):
                return target
            partial = self.cache_dir / f"{sha256}.{uuid.uuid4().hex}.part"
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                async with self.client_factory(timeout=httpx.Timeout(30, connect=10),
                                               follow_redirects=False,
                                               headers={"User-Agent": "StoryEngine/1.0 (ccMixter music cache)",
                                                        "Referer": "https://ccmixter.org/"}) as client:
                    # Follow a small number of redirects only within the official host.
                    for redirect in range(4):
                        async with client.stream("GET", url) as response:
                            if response.is_redirect:
                                location = str(response.url.join(response.headers.get("location", "")))
                                if redirect == 3 or not valid_source(location, sha256, size):
                                    raise MusicUnavailable("Invalid music redirect")
                                url = location
                                continue
                            response.raise_for_status()
                            digest = hashlib.sha256()
                            count = 0
                            with partial.open("wb") as output:
                                async for chunk in response.aiter_bytes():
                                    count += len(chunk)
                                    if count > size:
                                        raise MusicUnavailable("Music file exceeds expected size")
                                    output.write(chunk)
                                    digest.update(chunk)
                            if count != size or digest.hexdigest() != sha256:
                                raise MusicUnavailable("Music file failed integrity check")
                            return self._publish(partial, target, sha256, size)
                raise MusicUnavailable("Music download did not complete")
            except (httpx.HTTPError, OSError) as exc:
                raise MusicUnavailable("Music source or cache is unavailable") from exc
            finally:
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass


_ASSETS: MusicAssets | None = None


def get_music_assets() -> MusicAssets:
    global _ASSETS
    if _ASSETS is None:
        _ASSETS = MusicAssets()
    return _ASSETS
