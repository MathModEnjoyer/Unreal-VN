"""Check source-tree publication candidates without printing secret values.

This is a heuristic local check, not a complete secret detector. It never
changes files, initializes Git or contacts an external service.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re


SKIP = {'.git', 'node_modules', '.venv', 'venv', '__pycache__',
        '.pytest_cache', 'dist', 'build', '.idea', '.vscode'}
TEXT = {'.py', '.spec', '.js', '.jsx', '.css', '.json', '.jsonl', '.ipynb',
        '.md', '.txt', '.html', '.toml', '.yml', '.yaml', '.ini', '.cfg'}
TOKEN = re.compile(r'\b(?:sk-[A-Za-z0-9]{24,}|hf_[A-Za-z0-9]{25,}|gh[pousr]_[A-Za-z0-9]{25,})\b')
PERSONAL_PATH = re.compile(r'C:[\\/]+Users[\\/]+(?!Public\b|Default\b)[A-Za-z0-9_. -]+[\\/]', re.I)


def check(root: Path) -> tuple[list[str], list[str], int, int]:
    issues, excluded = [], []
    file_count = total_bytes = 0
    known_key = ''
    try:
        config = json.loads((Path.home() / '.story_engine/config.json').read_text(encoding='utf-8'))
        value = config.get('deepseek_api_key') if isinstance(config, dict) else None
        known_key = value if isinstance(value, str) else ''
    except (OSError, ValueError):
        pass
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            path = parent / name
            if name in SKIP:
                excluded.append(path.relative_to(root).as_posix())
                dirs.remove(name)
            elif path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                issues.append(f'{path.relative_to(root).as_posix()}: external/link directory')
                dirs.remove(name)
            elif name in {'logs', '.story_engine', 'sessions', 'saves'}:
                issues.append(f'{path.relative_to(root).as_posix()}: local player/log directory')
                dirs.remove(name)
        for name in files:
            path = parent / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                issues.append(f'{rel}: linked file')
                continue
            size = path.stat().st_size
            file_count += 1
            total_bytes += size
            if size >= 100 * 1024 * 1024:
                issues.append(f'{rel}: reaches GitHub 100 MiB file limit')
            if (name == 'config.json' or (name.startswith('.env') and name != '.env.example')
                    or path.suffix in {'.sqlite3', '.db', '.jsonl', '.zip', '.safetensors', '.ckpt'}):
                issues.append(f'{rel}: local data, archive or model file; inspect before publication')
            if path.suffix.lower() not in TEXT and size > 1024 * 1024:
                continue
            try:
                text = path.read_text(encoding='utf-8-sig')
            except (UnicodeError, OSError):
                continue
            if known_key and known_key in text:
                issues.append(f'{rel}: matches the locally configured API key')
            if TOKEN.search(text):
                issues.append(f'{rel}: token-shaped string')
            if PERSONAL_PATH.search(text):
                issues.append(f'{rel}: personal absolute Windows path')
            if path.suffix == '.ipynb':
                try:
                    notebook = json.loads(text)
                    if any(cell.get('outputs') or cell.get('execution_count') is not None
                           for cell in notebook.get('cells', [])):
                        issues.append(f'{rel}: notebook outputs or execution history')
                except (ValueError, AttributeError):
                    issues.append(f'{rel}: invalid notebook JSON')
    return issues, excluded, file_count, total_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    root = parser.parse_args().root.resolve()
    if not root.is_dir():
        parser.error('root must be an existing directory')
    issues, excluded, count, size = check(root)
    print(f'Checked {count} source/asset files, {size / 1024 / 1024:.1f} MiB.')
    if excluded:
        print('Excluded generated/dependency directories (keep out of Git): ' + ', '.join(sorted(excluded)))
    for issue in issues:
        print('REVIEW: ' + issue)
    print('FAILED: inspect findings before publication.' if issues else 'OK: no findings in checked files.')
    print('Heuristic check only; review the Git staged file list before pushing.')
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
