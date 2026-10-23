"""Локальный журнал HTTP-вызовов DeepSeek, отдельный от игровых JSONL."""

from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
from pathlib import Path
import sys
import threading


_WRITE_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)
_ATTEMPT_CONTEXT = ContextVar("deepseek_attempt_context", default=None)


@contextmanager
def request_attempt(generation_id: str, attempt: int):
    """Контекст одной попытки, изолированный от соседних задач и потоков."""
    token = _ATTEMPT_CONTEXT.set({"generation_id": generation_id, "attempt": attempt})
    try:
        yield
    finally:
        _ATTEMPT_CONTEXT.reset(token)


def current_attempt() -> dict:
    # Копия не позволяет вызывающему коду менять активный контекст.
    return dict(_ATTEMPT_CONTEXT.get() or {})


def default_log_dir() -> Path:
    root = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[1])
    return root / "logs" / "deepseek"


def write_record(record: dict, *, api_key: str, log_dir: str | Path | None = None) -> None:
    """Ошибка журнала не должна менять результат API или запускать платный ретрай."""
    try:
        now = datetime.now(timezone.utc)

        def redact(value):
            if isinstance(value, str):
                return value.replace(api_key, "[REDACTED]") if api_key else value
            if isinstance(value, dict):
                return {redact(str(key)): redact(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [redact(item) for item in value]
            if value is None or isinstance(value, (int, float, bool)):
                return value
            return redact(str(value))

        line = json.dumps(redact({"timestamp": now.isoformat(), **record}), ensure_ascii=False)
        directory = Path(log_dir) if log_dir is not None else default_log_dir()
        with _WRITE_LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / f"deepseek_{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
    except Exception:
        # Не выводим исключение: путь/сообщение тоже могут содержать ключ.
        _LOGGER.warning("Не удалось записать локальный журнал DeepSeek")
