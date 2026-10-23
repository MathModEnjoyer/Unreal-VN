"""Explicit model-selected background IDs and local asset validation."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from background_lighting import fallback_definition, time_mode_for
from background_effects import effect_profile_for

def _lighting_for(query: str) -> str:
    """Вариант фона определяется временем суток, независимо от погоды и тона."""
    words = set(re.findall(r"[а-яa-z0-9]+", str(query or "").lower().replace("ё", "е")))
    if words & {"ночь", "ночью", "полночь", "night", "midnight"} or any(
        word.startswith(("ночн", "полуноч", "лунн")) for word in words
    ):
        return "night"
    if words & {"вечер", "вечером", "сумерки", "evening", "dusk", "закат", "закате"} or any(
        word.startswith(("вечерн", "сумереч", "закат")) for word in words
    ):
        return "evening"
    if words & {"утро", "утром", "рассвет", "morning", "dawn"} or any(
        word.startswith(("утрен", "рассвет")) for word in words
    ):
        return "morning"
    return "day"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


@dataclass(frozen=True)
class VisualMatch:
    asset_id: str
    kind: str
    score: float
    matched_terms: tuple[str, ...]
    file: str | None = None
    environment: str | None = None
    worlds: tuple[str, ...] = ()
    time_of_day: str = "day"
    lighting_profile: str = "outdoor"
    time_mode: str = "outdoor"
    is_fallback: bool = False
    effect_profile: dict | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "score": self.score,
            "matched_terms": list(self.matched_terms),
            "file": self.file,
            "environment": self.environment,
            "worlds": list(self.worlds),
            "time_of_day": self.time_of_day,
            "lighting_profile": self.lighting_profile,
            "time_mode": self.time_mode,
            "is_fallback": self.is_fallback,
            "effect_profile": self.effect_profile or effect_profile_for({"id": self.asset_id, "is_fallback": self.is_fallback}),
            "url": f"/backgrounds/{self.file}" if self.file else None,
        }


class VisualCatalog:
    """Каталоги индексируются один раз; подбор не обращается к LLM."""

    def __init__(self, catalog_dir: str | Path | None = None):
        configured = catalog_dir or os.getenv("STORY_ENGINE_VISUAL_CATALOG_DIR")
        if configured:
            self.catalog_dir = Path(configured)
        else:
            self.catalog_dir = Path(__file__).with_name("visual_catalogs")
            if not self.catalog_dir.exists() and getattr(sys, "_MEIPASS", None):
                self.catalog_dir = Path(sys._MEIPASS) / "visual_catalogs"
        self.backgrounds = self._load_backgrounds()

    def _load_backgrounds(self) -> list[dict[str, Any]]:
        raw = _read_json(self.catalog_dir / "background_catalog.json", {})
        self.fallback = {**fallback_definition(), **(raw.get("fallback") or {})} if isinstance(raw, dict) else fallback_definition()
        if isinstance(raw, dict) and isinstance(raw.get("places"), dict):
            return [{"id": key, **value} for key, value in raw["places"].items() if key != "abstract_space" and not value.get("is_fallback")]
        return [{"id": key, **value} for key, value in raw.items() if key != "abstract_space" and isinstance(value, dict) and not value.get("is_fallback")] if isinstance(raw, dict) else []

    def menu_backgrounds(self) -> list[dict[str, str]]:
        """Small local catalog for random menus; only artwork supporting time grading."""
        from background_assets import background_directory
        directory = background_directory()
        result = []
        for item in self.backgrounds:
            mode = time_mode_for(item)
            filename = item.get('file', '')
            if (mode not in {'outdoor', 'windowed'} or
                    not isinstance(filename, str) or
                    not re.fullmatch(r'[a-z_]+__day\.(?:webp|png)', filename) or
                    not (directory / filename).is_file()):
                continue
            result.append({'asset_id': item['id'], 'url': f'/backgrounds/{filename}',
                           'time_mode': mode, 'lighting_profile': mode})
        return result

    def prompt_catalog(self):
        rows = [f"{item['id']}: {item.get('label') or item['id']}" for item in sorted(self.backgrounds, key=lambda item: item['id'])]
        return ("\n[КАТАЛОГ ФОНОВ — только для планирования событий]\n"
                + "\n".join(rows)
                + "\nДля первого/нового места укажи location.background_id: подходящий ID из каталога по смыслу или abstract_space, если подходящего нет. "
                "Для одного location.id сохраняй выбранный фон. Не выдумывай ID.\n")

    def rank_backgrounds(self, query, *, world=None, limit=5):
        return []  # Kept for old diagnostic clients; local ranking is removed.

    def recommend(self, query, *, world=None, lighting_query=None, location_query=None, background_id=None):
        from background_assets import background_directory
        light = _lighting_for(lighting_query or query)
        item = next((item for item in self.backgrounds if item['id'] == background_id), None)
        reason = 'model_fallback' if background_id == 'abstract_space' else 'no_selection'
        if background_id and not item and background_id != 'abstract_space':
            reason = 'invalid_background_id'
        if item:
            filename = item.get('file')
            if (isinstance(filename, str) and re.fullmatch(r'[a-z_]+__day\.(?:webp|png)', filename)
                    and (background_directory() / filename).is_file()):
                mode = time_mode_for(item)
                match = VisualMatch(asset_id=item['id'], kind='background', score=1.0, matched_terms=(),
                    file=filename, environment=item.get('environment'), worlds=tuple(item.get('worlds') or []),
                    time_of_day='neutral' if mode == 'fixed' else light, lighting_profile=mode,
                    time_mode=mode, effect_profile=effect_profile_for(item))
                return {'query': query, 'backgrounds': [match.as_dict()], 'requested_time_of_day': light, 'fallback': None}
            reason = 'asset_unavailable'
        match = VisualMatch(asset_id='abstract_space', kind='background', score=0.0, matched_terms=(),
            file=self.fallback.get('file'), environment='enclosed', time_of_day='neutral',
            lighting_profile='fixed', time_mode='fixed', is_fallback=True)
        return {'query': query, 'backgrounds': [match.as_dict()], 'requested_time_of_day': light,
                'fallback': {'reason': reason, 'nearest_candidates': []}}


_CATALOG: VisualCatalog | None = None


def get_visual_catalog() -> VisualCatalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = VisualCatalog()
    return _CATALOG


def visual_query(
    scene: dict[str, Any], *, genre: str = "", tone: str = "", events: Iterable[str] = (),
    recent_lines: Iterable[dict[str, Any]] = (),
) -> str:
    parts = [genre, tone, scene.get("location", ""), scene.get("narration", ""), scene.get("dialogue", "")]
    parts.extend(list(events)[-8:])
    for line in list(recent_lines)[-6:]:
        if isinstance(line, dict):
            parts.extend([line.get("speaker", ""), line.get("text", "")])
    return " ".join(str(part) for part in parts if part)
