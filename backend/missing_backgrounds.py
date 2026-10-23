"""Локальный журнал показанных сцен с резервным фоном."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def journal_path() -> Path:
    configured = os.getenv("STORY_ENGINE_MISSING_BACKGROUNDS_PATH")
    return Path(configured) if configured else Path.home() / ".story_engine" / "missing_backgrounds.sqlite3"


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class MissingBackgroundJournal:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else journal_path()

    def record(self, *, session_id: str, node_id: str | None, scene: dict,
               genre: str = "", tone: str = "", scene_number: int = 0,
               requested_time: str = "day", reason: str = "no_matches",
               nearest_candidates: list | None = None) -> bool:
        """Один узел сессии — одна запись, в том числе после перезапуска."""
        if not session_id:
            return False
        # Сохраняем только текущую сцену, без накопленных фактов и запросов LLM.
        fields = {key: str(scene.get(key) or "")[:16000]
                  for key in ("location", "narration", "dialogue", "time_of_day")}
        location = fields["location"].strip()
        group_id = _digest([_normalize(genre), _normalize(location) if location else _digest(fields)])
        event_id = _digest([session_id, "node", node_id] if node_id else [session_id, "scene", fields])
        entry = {
            "session_id": session_id, "node_id": node_id,
            "scene_number": scene_number, "genre": genre, "tone": tone,
            **fields, "requested_time_of_day": requested_time,
            "reason": reason, "nearest_candidates": nearest_candidates or [],
        }
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=2)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS occurrences (
                event_id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
                location TEXT NOT NULL, genre TEXT NOT NULL,
                recorded_at TEXT NOT NULL, payload TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS occurrences_group ON occurrences(group_id)")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO occurrences VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, group_id, location, genre, now, json.dumps(entry, ensure_ascii=False)),
            )
            return cursor.rowcount == 1

    def report(self) -> dict:
        groups = {}
        if self.path.exists():
            # Чтение не создаёт пустую БД и не меняет журнал.
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                rows = connection.execute(
                    "SELECT group_id, location, genre, recorded_at, payload FROM occurrences ORDER BY recorded_at, event_id"
                )
                for group_id, location, genre, recorded_at, payload in rows:
                    group = groups.setdefault(group_id, {
                        "id": group_id, "location": location or "Без указанной локации",
                        "genre": genre, "scene_count": 0,
                        "first_seen": recorded_at, "last_seen": recorded_at,
                        "reasons": {}, "times_of_day": {}, "scenes": [],
                    })
                    entry = json.loads(payload)
                    entry["recorded_at"] = recorded_at
                    group["scenes"].append(entry)
                    group["scene_count"] += 1
                    group["last_seen"] = recorded_at
                    for field, key in (("reasons", "reason"), ("times_of_day", "requested_time_of_day")):
                        value = entry[key]
                        group[field][value] = group[field].get(value, 0) + 1
        locations = sorted(groups.values(), key=lambda item: (-item["scene_count"], item["location"], item["id"]))
        return {
            "schema_version": 1, "journal_path": str(self.path),
            "location_count": len(locations),
            "scene_count": sum(item["scene_count"] for item in locations),
            "locations": locations,
        }


def markdown_report(report: dict) -> str:
    def plain(value) -> str:
        # Текст истории остаётся текстом, включая строки с Markdown/HTML.
        text = re.sub(r"\s+", " ", str(value)).strip()
        return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", text)

    reasons = {
        "no_matches": "не найден подходящий фон",
        "low_confidence": "слишком слабое совпадение с каталогом",
        "catalog_unavailable": "каталог недоступен: проверить ошибку до генерации",
    }
    lines = ["# Сцены с заглушкой", "",
             f"Локаций: {report['location_count']}. Сцен: {report['scene_count']}.", "",
             "Группы объединены по названию места и жанру. Перед генерацией проверьте примеры: иногда достаточно исправить подбор или ключевые слова существующего фона.", "",
             "Для каждого нового фона генерируем дневной оригинал. Время суток обрабатывает игра; для закрытых помещений можно выбрать фиксированное освещение.", ""]
    for group in report["locations"]:
        lines += [f"## {plain(group['location'])}", "",
                  f"Жанр: {plain(group['genre']) or 'не указан'}. Сцен: {group['scene_count']}.", "",
                  "Причины: " + "; ".join(f"{reasons.get(key, plain(key))}: {count}" for key, count in group["reasons"].items()) + ".", "",
                  "Запрошенное время: " + ", ".join(f"{plain(key)}: {count}" for key, count in group["times_of_day"].items()) + ".", ""]
        for scene in group["scenes"][:3]:
            lines += [f"- Сессия {plain(scene['session_id'])}, узел {plain(scene['node_id'] or 'без ID')}, сцена {scene['scene_number']}.",
                      f"  Описание: {plain(scene['narration']) or 'не указано'}",
                      f"  Диалог: {plain(scene['dialogue']) or 'нет'}", ""]
        if group["scene_count"] > 3:
            lines += ["Остальные примеры доступны в JSON\u002dвыгрузке.", ""]
    return "\n".join(lines)
