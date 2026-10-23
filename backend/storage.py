"""Atomic replacement: a failed write never truncates the last valid save."""

import json
import os
import tempfile
from pathlib import Path


def atomic_json(path, data):
    path = Path(path)
    # Serialize before opening any file, including when data is not JSON-safe.
    content = json.dumps(data, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
