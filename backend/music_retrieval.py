"""Подбор лицензированной музыки по состоянию сцены.

Подбор по текущей сцене, жанру и тону без сети и запросов к языковой модели.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from music_assets import valid_source

TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.I)
ALLOWED_LICENSES = {"CC0", "CC-BY", "CC BY", "CC-BY-4.0", "CC BY 4.0", "CC-BY-3.0", "own"}
GENRES = {
    "фэнтези": {"fantasy", "orchestral", "ambient"}, "фантастика": {"scifi", "electronic"},
    "киберпанк": {"cyberpunk", "electronic", "synthwave"}, "хоррор": {"horror", "dark", "drone"},
    "детектив": {"mystery", "noir", "jazz"}, "романтика": {"romantic", "piano", "strings"},
    "исторический": {"historical", "acoustic", "orchestral"},
    "sci-fi": {"scifi", "electronic", "ambient"}, "scifi": {"scifi", "electronic", "ambient"},
    "драма": {"drama", "piano", "cinematic"}, "приключение": {"adventure", "orchestral"},
    "мистика": {"mystery", "ambient"}, "производственный роман": {"everyday", "calm", "acoustic"},
}
MOOD_CUES = {
    "action": (r"бой|бо[яю]|битв.*|сраж.*|погон.*|стрел.*|выстрел.*|атак.*|драк.*", .9),
    "horror": (r"ужас.*|чудовищ.*|кошмар.*|монстр.*|жутк.*", .75),
    "tense": (r"опасн.*|угроз.*|тревож.*|напряж.*|ловушк.*|преслед.*", .65),
    "sad": (r"груст.*|печал.*|утрат.*|плач.*|скорб.*|одиночеств.*|похорон.*", .2),
    "romantic": (r"романт.*|любов.*|нежн.*|поцелу.*|свидани.*", .25),
    "mystery": (r"тайн.*|расслед.*|загад.*|улик.*|подозрен.*", .4),
    "joyful": (r"радост.*|весел.*|праздник.*|торжеств.*|побед.*", .5),
    "calm": (r"спокойн.*|уют.*|отдых.*|повседневн.*|мирн.*", .2),
}


def music_query(scene: dict) -> str:
    """Только текущая сцена: прошлый бой не окрашивает мирный разговор."""
    return " ".join(str(scene.get(key) or "") for key in ("location", "narration", "dialogue"))


def _music_features(text, genre, tone):
    words = _tokens(text)
    # Убираем простые отрицания, чтобы «без опасности» не включало тревожную музыку.
    clean = []
    for i, word in enumerate(words):
        before = " ".join(words[max(0, i - 2):i])
        if re.search(r"(?:без|нет|никакой|никакого|никаких)$", before):
            continue
        clean.append(word)
    moods = [mood for mood, (pattern, _) in MOOD_CUES.items()
             if any(re.fullmatch(pattern, word) for word in clean)]
    tags = set()
    for name, mapped in GENRES.items():
        if name in genre.lower():
            tags.update(mapped)
    preferred = moods[0] if moods else None
    if not preferred:
        if "хоррор" in genre.lower(): preferred = "horror"
        elif "детектив" in genre.lower() or "мистика" in genre.lower(): preferred = "mystery"
        elif "напряж" in tone or "мрач" in tone: preferred = "tense"
        elif "меланхол" in tone: preferred = "sad"
        else: preferred = "calm"
    tags.add(preferred)
    return tags, MOOD_CUES[preferred][1], preferred


def _tokens(value: Any) -> list[str]:
    return [x.lower().replace("ё", "е") for x in TOKEN_RE.findall(str(value or "")) if len(x) > 2]


def _cosine(a: Counter[str], b: Counter[str], idf: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    an = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in a.items()))
    bn = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in b.items()))
    return 0.0 if not an or not bn else sum(a[k] * b.get(k, 0) * idf.get(k, 1.0) ** 2 for k in a) / (an * bn)


@dataclass(frozen=True)
class MusicTrack:
    id: str
    title: str
    file: str | None = None
    url: str | None = None
    source: str | None = None
    license: str = "unknown"
    attribution: str | None = None
    tags: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    moods: tuple[str, ...] = ()
    worlds: tuple[str, ...] = ()
    bpm: float | None = None
    intensity: float = .5
    loopable: bool = False
    download_url: str | None = None
    sha256: str | None = None
    byte_size: int | None = None
    artist: str | None = None
    contributors: str | None = None
    license_url: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MusicTrack":
        seq = lambda key: tuple(map(str, raw.get(key, []) or []))
        bpm = raw.get("bpm")
        return cls(str(raw.get("id")), str(raw.get("title") or raw.get("name") or raw.get("id")),
                   raw.get("file"), raw.get("url"), raw.get("source"), str(raw.get("license", "unknown")),
                   raw.get("attribution"), seq("tags"), seq("genres"), seq("moods"), seq("worlds"),
                   float(bpm) if bpm is not None else None, max(0., min(1., float(raw.get("intensity", .5)))),
                   bool(raw.get("loopable", False)), raw.get("download_url"), raw.get("sha256"),
                   raw.get("byte_size"), raw.get("artist"), raw.get("contributors"), raw.get("license_url"))

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "file": self.file, "url": self.url,
                "source": self.source, "license": self.license, "attribution": self.attribution,
                "tags": list(self.tags), "genres": list(self.genres), "moods": list(self.moods),
                "worlds": list(self.worlds), "bpm": self.bpm, "intensity": self.intensity,
                "loopable": self.loopable, "artist": self.artist,
                "contributors": self.contributors, "license_url": self.license_url}


class MusicCatalog:
    def __init__(self, path: str | Path | None = None):
        configured = path or os.getenv("STORY_ENGINE_MUSIC_CATALOG")
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        self.path = Path(configured) if configured else root / "music_catalogs/music_catalog.json"
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        raw = raw.get("tracks", []) if isinstance(raw, dict) else raw
        self.tracks = [MusicTrack.from_dict(x) for x in raw if isinstance(x, dict) and x.get("id")]
        self.tracks = [x for x in self.tracks if x.license.strip() in ALLOWED_LICENSES]
        # Missing bundled assets cannot become a recommendation with a broken audio URL.
        available = []
        for track in self.tracks:
            if track.download_url and not valid_source(track.download_url, track.sha256, track.byte_size):
                continue
            if track.file:
                target = (self.path.parent / track.file).resolve()
                if not target.is_relative_to(self.path.parent.resolve()) or not target.is_file():
                    continue
            available.append(track)
        self.tracks = available
        self.vectors = [Counter(_tokens(" ".join((x.title, *x.tags, *x.genres, *x.moods)))) for x in self.tracks]
        frequency = Counter(k for v in self.vectors for k in v)
        n = max(len(self.vectors), 1)
        self.idf = {k: math.log((1 + n) / (1 + v)) + 1 for k, v in frequency.items()}

    @staticmethod
    def features(text: str, genre: str, tone: str) -> tuple[set[str], float]:
        tags, intensity, _ = _music_features(text, genre, tone)
        return tags, intensity

    def recommend(self, text: str, *, genre: str = "", tone: str = "", world: str | None = None,
                  recent_ids: Iterable[str] = (), limit: int = 3) -> dict[str, Any]:
        tags, intensity, preferred = _music_features(text, genre, tone)
        query = Counter(_tokens(" ".join((text, genre, tone, *tags))))
        recent = set(recent_ids); ranked = []
        for track, vector in zip(self.tracks, self.vectors):
            if world and track.worlds and world not in track.worlds: continue
            if preferred not in track.moods:
                continue
            track_tags = {x.lower() for x in (*track.tags, *track.genres, *track.moods)}
            matches = sorted(set(query) & set(vector))
            # Один лишь похожий темп не должен включать случайную музыку:
            # нужен хотя бы один смысловой тег или слово из описания сцены.
            if not matches and not (tags & track_tags):
                continue
            score = _cosine(query, vector, self.idf) + min(.35, .07 * len(matches))
            score += min(.22, .055 * len(tags & track_tags))
            score += max(0., .18 - abs(track.intensity - intensity) * .35)
            if world and world in track.worlds: score += .08
            if track.loopable: score += .025
            if track.id in recent: score -= .16
            ranked.append((score, track, matches))
        ranked.sort(key=lambda x: (-x[0], x[1].id)); selected = ranked[:max(1, limit)]
        return {"query": {"text": text, "genre": genre, "tone": tone, "tags": sorted(tags),
                          "preferred_mood": preferred, "target_intensity": round(intensity, 3)},
                "tracks": [{**t.as_dict(), "score": round(max(0., min(1., s)), 4), "matched_terms": m} for s, t, m in selected],
                "selected": selected[0][1].id if selected else None, "silence": not bool(selected)}


_CATALOG: MusicCatalog | None = None


def get_music_catalog() -> MusicCatalog:
    global _CATALOG
    if _CATALOG is None: _CATALOG = MusicCatalog()
    return _CATALOG
