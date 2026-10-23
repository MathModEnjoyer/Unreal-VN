# -*- mode: python ; coding: utf-8 -*-
#
# Сборка: pyinstaller --distpath ../dist run.spec  (запускать из backend/, на Windows)
# Результат: ../dist/StoryEngine.exe — один файл, Python на целевой машине не нужен.
#
# hidden_imports нужны, потому что uvicorn подгружает часть своих модулей
# динамически (через importlib) в зависимости от платформы и установленных
# расширений — PyInstaller анализирует граф импортов статически и такие
# модули не увидит сам. Без этого списка сборка либо не запустится вообще,
# либо упадёт в рантайме на первом запросе с ImportError вместо явной ошибки
# сборки — отлаживать такое труднее, чем один раз перечислить явно.

from pathlib import Path
import ssl

# Some Python distributions load additional TLS trust data from beside ssl.py.
# Keep that data beside the frozen module too; collecting certifi is insufficient.
ssl_data = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
runtime_datas = [(str(ssl_data), '.')] if ssl_data.is_file() else []

hidden_imports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # FastAPI импортирует python_multipart ВНУТРИ функции, а не на уровне
    # модуля — статический анализ PyInstaller такой импорт не видит. Без
    # этой строки собранный .exe падал бы на загрузке .jsonl («продолжить
    # историю из файла») с невнятным ImportError, хотя пакет установлен.
    "python_multipart",
    "python_multipart.multipart",
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    # Собранный фронтенд едет внутрь .exe как данные, а не как код — тот же
    # путь, что server.py ищет через sys._MEIPASS в собранном режиме
    # (см. _resolve_frontend_dist в server.py).
    datas=[
        ("../frontend/dist", "frontend_dist"),
        ("music_catalogs", "music_catalogs"),
        ("backgrounds", "backgrounds"),
        ("ambience", "ambience"),
        ("visual_catalogs", "visual_catalogs"),
        ("character_data", "character_data"),
        ("../assets/characters/cutouts", "characters/cutouts"),
    ] + runtime_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="StoryEngine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # видно вывод сервера и возможные ошибки — полезно для
    # личного инструмента; False сделает окно консоли скрытым, но тогда
    # любая ошибка запуска останется невидимой пользователю
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
