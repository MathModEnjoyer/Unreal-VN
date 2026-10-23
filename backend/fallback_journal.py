"""Deduplicated missing art and technical failures, independent of story saves."""
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class FallbackJournal:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('STORY_ENGINE_FALLBACK_LOG_PATH') or
                         Path.home() / '.story_engine' / 'fallbacks.sqlite3')

    def record(self, *, session_id, node_id, kind, subject, reason, context=None):
        if not session_id or not node_id:
            return False
        category = 'technical' if reason in {'asset_unavailable', 'catalog_unavailable', 'image_load_failed', 'canvas_failed'} else 'content'
        key = hashlib.sha256(json.dumps([session_id, node_id, kind, subject, category]).encode()).hexdigest()
        payload = dict(session_id=session_id, node_id=node_id, kind=kind, subject=subject,
                       reason=reason, category=category, context=context or {},
                       recorded_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=2)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS fallbacks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            return connection.execute('INSERT OR IGNORE INTO fallbacks VALUES (?, ?)',
                                      (key, json.dumps(payload, ensure_ascii=False))).rowcount == 1

    def report(self):
        entries = []
        if self.path.exists():
            with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as connection:
                entries = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM fallbacks ORDER BY rowid DESC')]
        groups = {}
        for entry in entries:
            key = (entry['session_id'], entry['kind'], entry['subject'], entry['reason'])
            group = groups.setdefault(key, {**entry, 'count': 0})
            group['count'] += 1
        return {'schema_version': 1, 'scene_count': len(entries), 'groups': list(groups.values()), 'entries': entries}
