"""
Локальное хранение ключей API. Файл лежит в домашней директории пользователя,
никуда не отправляется — это то же самое доверие, что уже требуется от
переменных окружения, просто удобнее: ключ вводится один раз через интерфейс
и переживает перезапуск сервера без повторного export в терминале.

Права доступа к файлу выставляются в 0600 (чтение/запись только владельцу) —
минимальная защита, уместная для локального личного инструмента.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from storage import atomic_json

class ConfigWriteError(Exception):
    """Не удалось записать конфиг на диск — сообщаем игроку, а не глотаем."""


CONFIG_PATH = Path.home() / ".story_engine" / "config.json"

KNOWN_KEYS = ("deepseek_api_key",)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    # Файл мог содержать валидный JSON, но не объект: "null", список,
    # число. Вызывающий код ждёт словарь и падал бы на .get() с
    # AttributeError — битый конфиг не должен ронять сервер.
    return data if isinstance(data, dict) else {}


def save_partial(updates: dict) -> dict:
    """
    Мержит переданные поля с уже сохранёнными — пустая строка или None
    удаляет ключ (это единственный способ его стереть через интерфейс),
    отсутствие поля в updates оставляет существующее значение как есть.
    """
    current = load_config()
    for key in KNOWN_KEYS:
        if key in updates:
            value = updates[key]
            if value:
                current[key] = value
            else:
                current.pop(key, None)

    # Ошибка записи (диск полон, нет прав, путь занят) должна дойти до
    # игрока понятным сообщением, а не пятисоткой: он вводит ключ API и
    # вправе знать, сохранился тот или нет.
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(CONFIG_PATH, current)
    except OSError as e:
        raise ConfigWriteError(f"Не удалось сохранить настройки: {e}") from e

    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass  # платформы без chmod (напр. некоторые Windows-FS) — не критично
    return current


def status() -> dict:
    """Только факт наличия ключа, не само значение — для отображения в UI."""
    cfg = load_config()
    return {key: bool(cfg.get(key)) for key in KNOWN_KEYS}
