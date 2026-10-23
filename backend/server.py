"""
Самодостаточный локальный веб-сервер: держит и API, и собранный фронтенд
в одном процессе на одном порту. Открываешь браузер на http://<host>:8756 —
и бэкенд, и интерфейс уже там, без отдельного dev-сервера для фронта.

Работает где угодно, где есть Python — включая Termux на Android. С iOS
или другого устройства в той же сети заходишь по IP хоста, никакой
установки не требуется, это просто страница в браузере.

Запуск на телефоне (Termux) или десктопе, чтобы открывать только локально:
    uvicorn server:app --host 127.0.0.1 --port 8756

Запуск, чтобы открывать с других устройств в локальной сети (например,
зайти с айфона на бэкенд, который крутится в Termux на Android):
    uvicorn server:app --host 0.0.0.0 --port 8756
    # затем на другом устройстве: http://<IP-этого-хоста>:8756

ВНИМАНИЕ про 0.0.0.0: в этом режиме сервер доступен всем в локальной сети,
а он рассчитан на доверенного игрока и НЕ имеет ни авторизации, ни защиты
от злоупотреблений. Конкретно: поле «куда выгружать лог» позволит любому
в сети писать файлы в произвольную папку от имени этого процесса, а
запросы к генерации — тратить ваш ключ DeepSeek. Поднимайте на 0.0.0.0
только в сети, которой доверяете (домашний Wi-Fi), не в публичной.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
import sys
import uuid as uuid_lib
from collections import OrderedDict
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, File, UploadFile, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from character_sprites import assign_sprites, scene_sprites, get_sprite_catalog
from fallback_journal import FallbackJournal
from prose_blocks import normalize_prose
from character_identity import prepare_cast
from state_machine import StoryState, AgeValidationError
from deepseek_client import DeepSeekClient
from deepseek_logging import request_attempt
from prompts import cast_size_range, choice_windows, GENRE_DIRECTIVES, ALL_TONES
from story_events import event_text, event_prompt, bind_event, terminal_event, through_ending, restore_event_path
import config_store
from visual_retrieval import get_visual_catalog, visual_query, _lighting_for
from missing_backgrounds import MissingBackgroundJournal, markdown_report
from music_retrieval import get_music_catalog, music_query
from music_assets import get_music_assets, MusicUnavailable
from background_assets import background_directory
from background_effects import resolve_scene_effects, effect_scene_key
from scene_locations import restore_location_path, place_id
from scene_conditions import initial_conditions

app = FastAPI(title="Story Engine Backend")


def _scene_with_visuals(state: StoryState, scene: dict, *, session_id: str = "") -> dict:
    """Добавляет рекомендации ассетов, не меняя сохранённую сцену дерева."""
    query = ""
    explicit_time = scene.get("time_of_day")
    lighting_query = explicit_time if explicit_time in ("morning", "day", "evening", "night") else " ".join(
        str(scene.get(key) or "") for key in ("time_of_day", "location", "narration", "dialogue"))
    try:
        query = visual_query(
            scene,
            genre=state.genre,
            tone=state.tone,
            events=[event_text(record["event"]) for record in getattr(state, "event_history", [])
                    if "event" in record],
            recent_lines=state.recent_lines,
        )
        # Освещение берём из текущей сцены: прошлая ночь в фактах не должна
        # превращать явно утреннюю сцену в ночную.
        visual = get_visual_catalog().recommend(query, lighting_query=lighting_query,
            location_query=str(scene.get("location") or ""), background_id=scene.get("location_background_id"))
    except Exception:
        # Каталог необязателен: отсутствие JSON/PNG не должно ломать историю.
        visual = {"query": "", "backgrounds": [], "error": "catalog_unavailable"}
    backgrounds = visual.get("backgrounds") or []
    visual["effects"] = resolve_scene_effects(
        scene, backgrounds[0] if backgrounds else None,
        time_of_day=visual.get("requested_time_of_day") or _lighting_for(lighting_query),
        genre=state.genre,
    )
    visual["effects"]["scene_key"] = effect_scene_key(session_id, getattr(state, "tree_current_id", None), scene)
    if session_id and (not backgrounds or backgrounds[0].get("is_fallback")):
        fallback_info = visual.get("fallback") or {}
        try:
            MissingBackgroundJournal().record(
                session_id=session_id, node_id=getattr(state, "tree_current_id", None),
                scene=scene, genre=state.genre, tone=state.tone,
                scene_number=getattr(state, "delivered_scenes", 0),
                requested_time=visual.get("requested_time_of_day") or _lighting_for(lighting_query),
                reason=fallback_info.get("reason") or visual.get("error") or "no_matches",
                nearest_candidates=fallback_info.get("nearest_candidates"),
            )
        except Exception:
            # Сбой журнала не мешает читать историю; ошибка видна в логе сервера.
            logging.getLogger(__name__).exception("Failed to record missing background")
            visual["missing_background_log_error"] = True
    try:
        music = get_music_catalog().recommend(music_query(scene), genre=state.genre, tone=state.tone, limit=3)
    except Exception:
        music = {"query": {}, "tracks": [], "selected": None, "silence": True,
                 "error": "music_catalog_unavailable"}
    result = dict(scene)
    result["visual"] = visual
    result["music"] = music
    assign_sprites(state)
    result['characters'] = scene_sprites(state, scene)
    try:
        journal = FallbackJournal()
        context = {'location': scene.get('location'), 'narration': scene.get('narration'),
                   'scene_number': getattr(state, 'delivered_scenes', 0), 'background_id': scene.get('location_background_id')}
        if session_id and (not backgrounds or backgrounds[0].get('is_fallback')):
            journal.record(session_id=session_id, node_id=state.tree_current_id, kind='background',
                subject=str(scene.get('location_id') or scene.get('location') or 'unknown'),
                reason=(visual.get('fallback') or {}).get('reason') or visual.get('error') or 'no_selection', context=context)
        for character in result['characters']:
            if character['sprite'].get('is_fallback'):
                c = state.characters[character['id']]
                journal.record(session_id=session_id, node_id=state.tree_current_id, kind='character',
                    subject=character['id'], reason=character['sprite']['reason'],
                    context={**context, 'name': c.name, 'appearance': c.appearance,
                             'appearance_features': c.appearance_features, 'sprite_id': c.sprite_id})
    except Exception:
        logging.getLogger(__name__).exception('Failed to record fallback')
        result['fallback_log_error'] = True
    return result

# Разрешаем всё — сервер по умолчанию слушает только 127.0.0.1 (см. запуск
# выше), а при 0.0.0.0 это уже осознанный выбор для доступа из локальной сети.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware('http')
async def local_api_origin(request, call_next):
    """An unrelated website must not read local saves or spend the player's key."""
    from urllib.parse import urlsplit
    from fastapi.responses import JSONResponse
    protected = request.url.path.startswith(('/story/', '/settings', '/visual/', '/music/recommend'))
    origin = request.headers.get('origin')
    trusted = False
    if origin:
        try:
            parsed = urlsplit(origin)
            trusted = (origin == str(request.base_url).rstrip('/') or
                       (parsed.scheme in {'http', 'https'} and parsed.hostname in {'localhost', '127.0.0.1', '::1'}))
        except ValueError:
            pass
    if protected and ((origin and not trusted) or
                      (not trusted and request.headers.get('sec-fetch-site') == 'cross-site')):
        return JSONResponse({'detail': 'Доступ к локальной игре с постороннего сайта запрещён.'}, status_code=403)
    return await call_next(request)

# Дефолт для логов (транскрипт + диагностика), когда игрок не указал
# путь явно на экране онбординга — рабочий стол, чтобы файлы были на
# виду, а не спрятаны в скрытой папке профиля. Если Desktop не существует
# (headless-сервер, нетипичная система) — используем резервный путь,
# не падаем и не пытаемся создать папку с нестандартным именем силой.
_desktop = Path.home() / "Desktop"
SAVE_DIR = _desktop if _desktop.is_dir() else Path.home() / ".story_engine" / "saves"
SAVE_DIR.mkdir(parents=True, exist_ok=True)

# Активные сессии в памяти: session_id -> StoryState.
# Тоже LRU: вытеснение безопасно, потому что состояние автосохраняется на
# диск после каждой сцены, а _get_state умеет поднять его обратно. Лимит
# щедрый — запись весит ~9 КБ, это не картинки.
# Снимки сессий на диске. Состояние живёт в памяти процесса, и при падении
# бэкенда терялось бы всё — включая уже ОПЛАЧЕННОЕ построение дерева, которое
# стоит основную часть денег за историю. Снимок делается в двух точках:
# сразу после построения дерева и после каждой материализации сегмента, то
# есть ровно там, где только что потрачены токены.
SNAPSHOT_DIR = Path.home() / ".story_engine" / "sessions"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)



_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_session_id(session_id: str) -> str:
    """
    Проверяет, что идентификатор пригоден для имени файла.

    session_id приходит из URL и подставляется в пути снимков, логов и
    транскриптов. Сервер по умолчанию слушает только 127.0.0.1, но в
    документации прямо описан запуск на 0.0.0.0 — и тогда "../../etc/passwd"
    в адресе означал бы запись за пределы каталога. Свои id мы генерируем
    через uuid4, так что ограничение никому не мешает.
    """
    if not _SAFE_ID.match(session_id or ""):
        raise HTTPException(status_code=400, detail="Недопустимый идентификатор сессии")
    return session_id



def _snapshot_path(session_id: str) -> Path:
    return SNAPSHOT_DIR / f"{_safe_session_id(session_id)}.json"


def _repair_locked_choices(state: "StoryState") -> bool:
    """
    Чинит «запертые» узлы, сохранённые старой версией кода.

    Баг: если сегмент обрывался раньше конца (remaining_events непуст), а
    развилка была запланирована именно в последней применённой сцене, дети
    ей не подставлялись — scene.choices показывались игроку, но переход по
    ним был невозможен (children пуст), и история молча упиралась в финал.
    На живом прогоне так застряла сцена 12.

    В новой материализации это исправлено, но уже сохранённые снимки
    остались бы сломанными. Восстанавливаем по сохранённым
    segment_choice_texts/segment_children_ids: они есть у каждого узла-
    развилки, даже если children не проставили.

    Возвращает True, если что-то починил (нужно, чтобы закрепить снимок).
    """
    repaired = False
    for node in state.story_tree.values():
        if node.get("children") or node.get("validation_stopped") or node.get("generation_stopped"):
            continue
        if node.get("scene") is None:
            continue
        choice_texts = node.get("segment_choice_texts")
        children_ids = node.get("segment_children_ids") or {}
        if not choice_texts or not children_ids:
            continue
        valid = {c: children_ids[c] for c in choice_texts if c in children_ids}
        if valid:
            node["children"] = valid
            repaired = True
    return repaired


# Вызовы модели идут в потоках (httpx синхронный). Отдельный пул с
# фиксированным, но щедрым лимитом: asyncio.to_thread использует общий
# ThreadPoolExecutor на min(32, cpu+4) потоков, и на 4-ядерной машине это
# всего 8 слотов. Подготовка альтернатив и генерация прозы
# легко их выедают, и тогда следующий вызов ЖДЁТ свободный поток — тот
# самый внешне «мёртвый» висяк без сетевой активности.
#
# Отдельно важно: asyncio.wait_for вокруг to_thread НЕ освобождает поток
# при таймауте — поток продолжает жить и держать слот, а обёртка уже
# ушла. Несколько таких «отменённых» вызовов исчерпывают пул навсегда.
# Поэтому таймаут живёт в httpx (read=180), а не в wait_for.
_LLM_POOL = None


def _llm_pool():
    global _LLM_POOL
    if _LLM_POOL is None:
        import concurrent.futures
        max_workers = 16
        _LLM_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="llm"
        )
    return _LLM_POOL


async def _limited_llm_call(semaphore: asyncio.Semaphore, fn, *args, **kwargs):
    """Выполняет один вызов модели под лимитом, не удерживая слот рекурсией."""
    async with semaphore:
        return await _llm_call(fn, *args, **kwargs)


async def _llm_call(fn, *args, _attempt: int = 1, _generation_id: str | None = None, **kwargs):
    """
    Обёртка над blocking-вызовом модели, запускаемым в выделенном пуле.

    Таймаут обеспечивает сам httpx-клиент внутри client._call (read=180с):
    по истечении он бросает httpx.TimeoutException, поток освобождается,
    и ретраи на уровне вызывающего кода работают штатно.
    """
    loop = asyncio.get_running_loop()
    generation_id = _generation_id or uuid_lib.uuid4().hex

    def invoke():
        # Контекст устанавливается внутри потока: run_in_executor сам его
        # не переносит. Сброс обязателен при повторном использовании потока.
        with request_attempt(generation_id, _attempt):
            return fn(*args, **kwargs)

    return await loop.run_in_executor(_llm_pool(), invoke)


SNAPSHOT_KEEP = 40


def _prune_snapshots() -> None:
    """
    Оставляет только SNAPSHOT_KEEP самых свежих снимков.

    Каждая история оставляет файл на диске, а удаляется он только если игрок
    ЯВНО отказался продолжать. Брошенные (закрыл вкладку и забыл) копились
    бы навсегда: каждый снимок — это дерево с прозой, сотни килобайт.
    """
    try:
        files = sorted(SNAPSHOT_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old_file in files[SNAPSHOT_KEEP:]:
        try:
            old_file.unlink()
        except OSError:
            pass


def _save_snapshot(session_id: str, state: StoryState) -> None:
    """
    Пишет снимок молча. Ошибка записи (нет места, нет прав) не должна
    ронять уже сгенерированную сцену — игрок получит свой текст в любом
    случае, просто в этот раз прогресс не закрепится на диске.
    """
    try:
        state.save(str(_snapshot_path(session_id)))
        state._snapshot_error = None
    except (OSError, TypeError, ValueError):
        state._snapshot_error = "Не удалось сохранить прогресс на диск. Не закрывайте игру до восстановления записи."
        logging.getLogger(__name__).exception("Failed to save session %s", session_id)
        return
    _prune_snapshots()


SESSIONS_LIMIT = 50

# Блокировки по сессии. Без них два параллельных запроса (двойной клик,
# два окна, повторная отправка формы) могли одновременно войти в
# материализацию ОДНОГО сегмента — и оплатить генерацию дважды. Плюс
# счётчик прочитанных сцен рос на два за один реальный переход.
SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def _session_lock(session_id: str) -> asyncio.Lock:
    """Одна блокировка на сессию, создаётся при первом обращении."""
    lock = SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        SESSION_LOCKS[session_id] = lock
        # Чистим вместе с сессиями: держать блокировку для каждой истории,
        # когда-либо запущенной, — та же утечка, что была со статусами.
        for old_id, old_lock in list(SESSION_LOCKS.items()):
            if len(SESSION_LOCKS) <= SESSIONS_LIMIT * 2:
                break
            if old_id != session_id and not old_lock.locked() and not getattr(old_lock, '_waiters', None):
                SESSION_LOCKS.pop(old_id, None)
    return lock


SESSIONS: OrderedDict[str, StoryState] = OrderedDict()

# Построение дерева истории — фоновая задача (может занимать 1-3 минуты),
# не блокирует ответ /story/start. TREE_BUILD_TASKS хранит ссылку на саму
# asyncio.Task — без этого сборщик мусора мог бы прервать задачу до
# завершения, раз на неё больше никто явно не ссылается после return
# из эндпоинта. TREE_BUILD_STATUS — то, что реально опрашивает фронт.
TREE_BUILD_TASKS: dict[str, asyncio.Task] = {}
TREE_BUILD_STATUS: dict[str, dict] = {}

# Сессии вытесняются на SESSIONS_LIMIT, а эти два словаря росли без конца:
# на долгоживущем сервере статус каждой когда-либо запущенной истории
# оставался в памяти навсегда. Утечка медленная, но настоящая.
TREE_BUILD_TRACKING_LIMIT = 100


def _forget_old_builds() -> None:
    """
    Подрезает служебные словари построения. Порядок вставки в dict —
    хронологический, так что самые давние идут первыми; их и убираем.
    Завершённую задачу дополнительно снимаем с учёта: держать ссылку на
    объект Task после его завершения незачем.
    """
    for done_id in [k for k, t in TREE_BUILD_TASKS.items() if t.done()]:
        TREE_BUILD_TASKS.pop(done_id, None)
    while len(TREE_BUILD_STATUS) > TREE_BUILD_TRACKING_LIMIT:
        TREE_BUILD_STATUS.pop(next(iter(TREE_BUILD_STATUS)))
    while len(TREE_BUILD_TASKS) > TREE_BUILD_TRACKING_LIMIT:
        TREE_BUILD_TASKS.pop(next(iter(TREE_BUILD_TASKS)))

def _remember_session(session_id: str, state: StoryState) -> None:
    """
    Кладёт сессию в память с вытеснением самой давней. Вытесненная не
    теряется — она уже на диске, и _get_state поднимет её при обращении.
    """
    SESSIONS[session_id] = state
    SESSIONS.move_to_end(session_id)
    while len(SESSIONS) > SESSIONS_LIMIT:
        SESSIONS.popitem(last=False)


# ---------------------------------------------------------------- модели I/O


class StartRequest(BaseModel):
    # Затравка повторяется в контексте разных этапов; ограничиваем её размер.
    seed: str = Field(min_length=1, max_length=2000)
    genre: str = Field(min_length=1, max_length=60)
    tone: str = Field(min_length=1, max_length=60)
    target_length: int = Field(ge=3, le=150)
    clarifications: list[str] = Field(default_factory=list)  # ответы на уточняющие вопросы
    log_path: str | None = None  # куда игрок попросил выгружать диагностический лог
    # True: события двух ближайших альтернатив; False: только выбранной.
    build_all_branches: bool = True


class ClarifyRequest(BaseModel):
    seed: str = Field(min_length=1, max_length=2000)
    genre: str = Field(min_length=1, max_length=60)
    tone: str = Field(min_length=1, max_length=60)


class SceneRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    choice: str = Field(max_length=500)
    from_node_id: str | None = Field(default=None, min_length=1, max_length=64)


class VisualRecommendRequest(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    world: str | None = Field(default=None, max_length=40)
    background_id: str | None = Field(default=None, max_length=64)


class MusicRecommendRequest(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    genre: str = Field(default="", max_length=60)
    tone: str = Field(default="", max_length=60)
    world: str | None = Field(default=None, max_length=40)
    recent_ids: list[str] = Field(default_factory=list, max_length=20)


# ---------------------------------------------------------------- хелперы


def _check_scene_position(state: StoryState, node_id: str | None) -> None:
    # A queued click from another tab must not advance the newly current scene.
    if node_id is not None and node_id != state.tree_current_id:
        raise HTTPException(status_code=409, detail={
            "reason": "stale_scene",
            "message": "Сцена уже изменилась. Обновляем текущую позицию истории.",
        })


def _client(*, session_id: str | None = None) -> DeepSeekClient:
    cfg = config_store.load_config()
    try:
        return DeepSeekClient(api_key=cfg.get("deepseek_api_key"), session_id=session_id)
    except RuntimeError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "no_api_key",
                "message": "Ключ DeepSeek не задан. Откройте настройки и вставьте его.",
            },
        ) from e


@app.post("/visual/recommend")
async def recommend_visuals(req: VisualRecommendRequest):
    """Диагностический подбор без создания сессии и без вызова DeepSeek."""
    try:
        return get_visual_catalog().recommend(req.text, world=req.world, background_id=req.background_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Визуальный каталог недоступен") from exc


@app.get("/visual/menu-backgrounds")
async def menu_backgrounds():
    """Only asset metadata; selection and local time stay on the player's device."""
    return {"backgrounds": get_visual_catalog().menu_backgrounds()}


@app.get('/visual/fallbacks')
def fallback_report():
    try:
        return FallbackJournal().report()
    except Exception as exc:
        raise HTTPException(status_code=503, detail='Журнал заглушек недоступен') from exc


class FallbackReportRequest(BaseModel):
    session_id: str
    node_id: str
    kind: str
    subject: str
    reason: str


@app.post('/visual/fallbacks')
def report_fallback(req: FallbackReportRequest):
    state = _get_state(req.session_id)
    node = state.story_tree.get(req.node_id)
    if not node or not isinstance(node.get('scene'), dict):
        raise HTTPException(status_code=404, detail='Сцена не найдена')
    if req.kind not in {'character', 'background'} or req.reason not in {'image_load_failed', 'canvas_failed'}:
        raise HTTPException(status_code=422, detail='Неверная категория ошибки')
    if req.kind == 'character' and req.subject not in state.characters:
        raise HTTPException(status_code=422, detail='Неизвестный персонаж')
    scene = node['scene']
    FallbackJournal().record(session_id=req.session_id, node_id=req.node_id, kind=req.kind,
        subject=req.subject[:160], reason=req.reason,
        context={'location': scene.get('location'), 'narration': scene.get('narration')})
    return {'ok': True}


@app.get('/visual/fallbacks/export')
def export_fallbacks():
    return Response(json.dumps(fallback_report(), ensure_ascii=False, indent=2), media_type='application/json',
        headers={'Content-Disposition': 'attachment; filename="fallbacks.json"', 'Cache-Control': 'no-store'})


@app.get("/visual/missing-backgrounds")
def missing_backgrounds_report():
    """Список реально показанных сцен с заглушкой, сгруппированный по месту."""
    try:
        return MissingBackgroundJournal().report()
    except Exception as exc:
        logging.getLogger(__name__).exception("Failed to read missing background journal")
        raise HTTPException(status_code=503, detail="Журнал фонов недоступен") from exc


@app.get("/visual/missing-backgrounds/export")
def export_missing_backgrounds(format: str = "markdown"):
    """Выгрузка для проверки каталога и планирования новых дневных фонов."""
    if format not in {"markdown", "json"}:
        raise HTTPException(status_code=422, detail="Формат: markdown или json")
    report = missing_backgrounds_report()
    content = markdown_report(report) if format == "markdown" else json.dumps(report, ensure_ascii=False, indent=2)
    extension = "md" if format == "markdown" else "json"
    return Response(content, media_type="text/markdown" if format == "markdown" else "application/json",
                    headers={"Content-Disposition": f'attachment; filename="missing_backgrounds.{extension}"',
                             "Cache-Control": "no-store"})


@app.post("/music/recommend")
async def recommend_music(req: MusicRecommendRequest):
    """Диагностический подбор трека без создания сессии и без DeepSeek."""
    try:
        return get_music_catalog().recommend(
            req.text, genre=req.genre, tone=req.tone, world=req.world,
            recent_ids=req.recent_ids,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Музыкальный каталог недоступен") from exc


def _get_state(session_id: str) -> StoryState:
    """Текущая сессия из памяти или атомарного снимка на диске."""
    _safe_session_id(session_id)
    state = SESSIONS.get(session_id)
    if state:
        SESSIONS.move_to_end(session_id)
        return state

    path = _snapshot_path(session_id)
    if path.exists():
        try:
            state = StoryState.load(str(path))
            if _repair_locked_choices(state):
                # Ремонт изменил дерево — закрепляем на диске, чтобы не
                # повторять миграцию при каждом запуске.
                _save_snapshot(session_id, state)
            _remember_session(session_id, state)
            return state
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Раньше битый снимок молча превращался в 404 — игрок видел
            # «сессия не найдена», хотя файл на диске был, и причина
            # оставалась невидимой. Печатаем тип и текст: чаще всего это
            # рассинхрон схемы (поле появилось/исчезло между версиями).
            print(
                f"[story] snapshot {session_id} failed to load "
                f"({type(e).__name__}): {str(e).encode('ascii','replace').decode('ascii')}",
                flush=True,
            )

    raise HTTPException(status_code=404, detail="Сессия не найдена")


def _api_failure(exc: Exception, what: str) -> HTTPException:
    """
    Превращает сбой обращения к модели в понятный ответ вместо голой 500.

    Два реальных сценария, оба вне нашего контроля: сеть отвалилась
    (httpx.HTTPError) и модель вернула не-JSON, несмотря на response_format
    (json.JSONDecodeError) — у DeepSeek это случается. KeyError ловим тоже:
    структура ответа могла оказаться валидным JSON без нужных полей.
    """
    if isinstance(exc, OSError):
        message = "Не удалось загрузить сетевые файлы игры. Перезапусти игру из актуальной сборки."
        reason = "network_runtime_error"
    elif isinstance(exc, httpx.HTTPError):
        message = "Не удалось связаться с DeepSeek. Проверь сеть и ключ."
        reason = "api_unreachable"
    else:
        message = f"Модель вернула неожиданный ответ ({what}). Попробуй ещё раз."
        reason = "bad_model_response"
    return HTTPException(status_code=502, detail={"reason": reason, "message": message})


def _transcript_path(session_id: str) -> Path:
    return SAVE_DIR / f"{_safe_session_id(session_id)}_transcript.jsonl"


def _debug_log_path(session_id: str, log_dir: str | None) -> Path:
    """
    log_dir — папка, которую игрок явно указал перед стартом истории (см.
    поле "Куда выгружать диагностический лог" на экране онбординга). Если
    не указана (None) — используется папка сохранений по умолчанию, но
    это единственный фолбэк: если путь дан, пишем ТОЛЬКО туда, не туда
    и одновременно в старую папку "на всякий случай".
    """
    base = Path(log_dir).expanduser() if log_dir else SAVE_DIR
    # Игрок в интерфейсе вводит ПУТЬ К ПАПКЕ, но по факту может указать и
    # файл (напр. ...\se_run.jsonl). Раньше в этом случае создавалась ПАПКА
    # с именем файла, а внутрь неё падал <sid>_debug.jsonl — диагностика
    # «терялась» незаметно. Если у пути уже есть суффикс файла — пишем прямо
    # в него; если это существующая папка — в неё, как раньше.
    if log_dir and base.suffix:
        base.parent.mkdir(parents=True, exist_ok=True)
        return base
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{_safe_session_id(session_id)}_debug.jsonl"


def _log_debug(session_id: str, log_dir: str | None, step: str, payload: dict) -> None:
    """
    Диагностический лог — отдельный файл от игрового протокола
    (_append_transcript пишет то, что реально показано игроку; это —
    СЫРОЙ результат каждого отдельного вызова к модели, по одной строке
    на вызов, включая то, что могло быть отброшено защитным кодом дальше
    по пайплайну). Нужен, чтобы во время живого прогона можно было
    увидеть, что конкретно вернула модель на каждом шаге — план, каст,
    каждый сегмент дерева отдельно — не только финальный, уже
    обработанный результат.

    Как и автосохранение — не должен ронять генерацию при сбое диска,
    молча пропускает запись. Если указанная игроком папка недоступна на
    запись (опечатка в пути, нет прав) — тоже молча пропускает, а не
    роняет всё построение дерева из-за диагностики.
    """
    entry = {"ts": time.time(), "step": step, **payload}
    try:
        path = _debug_log_path(session_id, log_dir)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        # По-прежнему не роняем генерацию из-за диагностики, но и молчать
        # нельзя: без сообщения о сбое записи игрок считает, что лог ведётся,
        # и теряет данные, не понимая почему. Пишем один раз в stdout.
        print(f"[story] debug log write failed: {str(e).encode('ascii','replace').decode('ascii')}", flush=True)


def _append_transcript(session_id: str, entry: dict) -> None:
    """Дописывает реально показанную сцену; сбой диска не мешает чтению."""
    try:
        with open(_transcript_path(session_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[story] transcript write failed: {str(e).encode('ascii','replace').decode('ascii')}", flush=True)


@app.get("/health")
async def health():
    """Фронт дёргает это при старте, чтобы понять, поднялся ли бэкенд."""
    return {"status": "ok"}


class SettingsRequest(BaseModel):
    deepseek_api_key: str | None = Field(default=None, max_length=200)


@app.get("/settings")
async def get_settings():
    """
    Только факт наличия ключа (bool), не само значение — фронт хранит
    введённые ключи в полях формы сам, эндпоинт нужен лишь чтобы решить,
    показывать ли экран настроек при старте.
    """
    return config_store.status()


@app.post("/settings")
async def update_settings(req: SettingsRequest):
    """
    Пустая строка или отсутствие поля стирает соответствующий ключ —
    так фронт может очистить один ключ, не трогая остальные.
    """
    try:
        config_store.save_partial(req.model_dump(exclude_unset=True))
    except config_store.ConfigWriteError as e:
        raise HTTPException(
            status_code=500,
            detail={"reason": "config_write_failed", "message": str(e)},
        )
    return config_store.status()


@app.get("/story/premises")
async def premise_suggestions():
    """
    Три свежие затравки для экрана онбординга — генерируются заново при
    каждом открытии, не статичный список. Модерация входа здесь не нужна:
    нет пользовательского текста на этом шаге, только творческая генерация
    самой модели. При сбое интерфейс показывает ошибку и предлагает повторить
    загрузку; игрок также может написать свою затравку вручную.
    """
    client = _client()
    try:
        premises = await _llm_call(client.generate_premise_suggestions)
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, OSError) as e:
        raise _api_failure(e, "генерация затравок")
    return {"premises": premises}


class GenreToneFitRequest(BaseModel):
    seed: str = Field(min_length=1, max_length=2000)
    genre: str = Field(default="детектив", max_length=60)


@app.post("/story/genre_tone_fit")
async def genre_tone_fit(req: GenreToneFitRequest):
    """
    Сужает список жанров/тонов под конкретную затравку — если из текста уже
    ясно, что это хоррор, не показывать игроку "лёгкий" тон или "sci-fi"
    как равноценные варианты. Канонические списки — с бэкенда
    (GENRE_DIRECTIVES/ALL_TONES), не присылаются фронтом: фронт не должен
    быть источником истины о том, какие жанры вообще существуют.

    Затравка передаётся в подбор без отдельной модерации входа.
    Сам generate_genre_tone_fit уже сам
    откатывается к полным спискам при любом сбое (сеть, битый JSON, модель
    вернула слишком мало). Эндпоинту не нужен try/except поверх — метод
    подбора гарантирует валидный результат при сбое модели.
    """
    client = _client()
    genres, tones = await _llm_call(
        client.generate_genre_tone_fit, req.seed, list(GENRE_DIRECTIVES.keys()), ALL_TONES,
    )
    return {"genres": genres, "tones": tones}


@app.post("/story/clarify")
async def clarify(req: ClarifyRequest):
    """
    Шаг онбординга: по затравке генерируем уточняющие вопросы.
    Отдельной модерации затравки нет.
    """
    client = _client()

    try:
        questions = await _llm_call(
            client.generate_clarifying_questions, req.seed, req.genre, req.tone
        )
    except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
        raise _api_failure(e, "уточняющие вопросы")
    return {"questions": questions}


# ------------------------------------------------------ дерево истории
#
# Сегменты раскрываются по мере прохождения. Проза непосещённых веток не создаётся.

TREE_CONCURRENCY = 8  # верхний предел одновременных запросов к модели

# Сколько раз повторить ВСЮ попытку построения (персонажи + дерево + обзор)
# при сбое, прежде чем сдаться. Не фолбэк на другой режим генерации —
# именно повтор того же самого с чистого листа. Каждая попытка стоит
# полной цены дерева заново, поэтому число не завышено.
MAX_TREE_BUILD_ATTEMPTS = 2

# Повторяем генерацию событий только при сбое API или некорректном ответе,
# без отдельного редактора плана и повторов по его замечаниям.
MAX_EVENT_GENERATION_ATTEMPTS = 3

def _resolve_pivot_choices(
    raw_pivot: list[str] | None, window: tuple[int, int] | None,
    events_count: int, scene_before_segment: int,
) -> list[str] | None:
    """
    Защита контракта окна — единственное место, где она реально считается.
    И построение дерева (_build_tree_events), и восстановление сессии из
    .jsonl (_reconstruct_session) обязаны использовать именно эту функцию,
    не пересчитывать логику каждый по-своему — иначе восстановленная из
    лога сессия могла бы дать другой результат, чем сама генерация в своё
    время, что сделало бы восстановление ненадёжным.
    """
    if not raw_pivot or len(raw_pivot) < 2:
        return None
    candidate = raw_pivot[:2]
    if window is None:
        return None
    w_start, _w_end = window
    pivot_scene_number = scene_before_segment + events_count
    return candidate if pivot_scene_number >= w_start else None


def _segment_after(window: tuple[int, int] | None, target_length: int) -> tuple[int, tuple[int, int] | None] | None:
    """
    Что идёт СЛЕДОМ за сегментом, нацеленным на данное окно — используется
    и когда реальный выбор реально нашёлся (рекурсия в детей), и когда окно
    исчерпалось без выбора (graceful-деградация, продолжаем линейно к
    следующей границе как будто ничего не случилось). None означает "это
    был уже финальный сегмент, после него нечему следовать — история
    заканчивается здесь".

    Работает с любым числом окон (см. choice_window_count): находит текущее
    окно в списке и возвращает следующее, а для последнего — финальный
    участок до конца истории.
    """
    windows = choice_windows(target_length)
    try:
        idx = windows.index(window)
    except ValueError:
        return None  # окно не из этого набора (или None) — следующего нет
    if idx + 1 < len(windows):
        nxt = windows[idx + 1]
        return nxt[1], nxt
    return target_length, None


def _make_pending_branch(
    tree: dict, branch_state: StoryState, choice: str,
    ceiling_scene: int, window: tuple[int, int] | None,
) -> str:
    """
    Создаёт узел «отложенной» ветки — в экономичном режиме, когда заранее
    строится только проходимая сторона развилки.

    Узел пустой (событий нет), но помечен pending_branch=True и хранит
    ВСЁ нужное, чтобы достроить его лениво при реальном выборе: снимок
    состояния ветки, потолок и окно следующего сегмента. Это принципиально
    дешевле, чем строить ветку вслепую: платим только за то, что игрок
    реально выбрал.
    """
    node_id = uuid_lib.uuid4().hex
    tree[node_id] = {
        "scene": None, "materialized": False,
        "pending_branch": True,
        # Accepted history is rebuilt from the selected path. Keep only
        # planning coordinates, not another copy of the whole game state.
        "pending_state": {
            "planned_events_so_far": ([f"[Выбор героя] {choice}"]
                                      if choice and not choice.startswith("(") else []),
            "segment_index": branch_state.segment_index,
            "branch_path": list(branch_state.branch_path),
        },
        "pending_ceiling": ceiling_scene,
        "pending_window": list(window) if window else None,
        "origin_choice": choice,
        "segment_index": branch_state.segment_index,
        "branch_path": list(branch_state.branch_path),
    }
    return node_id


async def _build_pending_branch(session_id: str, state: StoryState, node_id: str) -> str:
    """
    Достраивает отложенную ветку по реальному выбору игрока.

    В экономном режиме вызывается после выбора. В обычном — для двух
    ближайших альтернатив. Каждый вызов строит ровно один сегмент событий
    и оставляет его дальнейшие продолжения отложенными.

    Возвращает id ПЕРВОГО узла построенного сегмента (он же заменяет
    pending-узел в дереве — id остаётся прежним, чтобы уже проставленные
    ссылки children не чинить).
    """
    node = state.story_tree[node_id]
    restore_event_path(state, state.story_tree, node_id)
    saved_plan = node["pending_state"]  # also accepts old full-state snapshots
    # Keep the planned path/decision, but use accepted source history from now.
    pending_state = copy.deepcopy(state)
    pending_state.story_tree = {}
    pending_state.planned_events_so_far = list(saved_plan.get("planned_events_so_far", []))
    pending_state.build_all_branches = state.build_all_branches
    pending_state.segment_index = saved_plan.get("segment_index", node.get("segment_index", 0))
    pending_state.pending_choice = node.get("origin_choice")
    pending_state.branch_path = list(saved_plan.get("branch_path", node.get("branch_path", [])))
    ceiling = node.get("pending_ceiling")
    window_raw = node.get("pending_window")
    window = tuple(window_raw) if window_raw else None
    origin_choice = node.get("origin_choice")

    client = _client(session_id=session_id)
    # Ограничитель применяется к отдельным вызовам модели внутри
    # _build_tree_events и не удерживается во время рекурсивного обхода.
    # Поэтому даже при одном слоте дочерняя ветка не блокируется родителем.
    semaphore = asyncio.Semaphore(TREE_CONCURRENCY)
    new_id = await _build_tree_events(
        pending_state, client, state.story_tree, semaphore,
        session_id, state.log_path, ceiling, window, origin_choice,
    )
    if state.story_tree[new_id].get('generation_error'):
        state.story_tree.pop(new_id)
        for key in ("total_input_tokens", "total_output_tokens", "total_api_calls",
                    "total_cache_hit_tokens", "total_cache_miss_tokens"):
            setattr(state, key, getattr(pending_state, key))
        _save_snapshot(session_id, state)
        raise HTTPException(status_code=502, detail={
            "reason": "generation_failed",
            "message": "Не удалось получить события продолжения. Ветка сохранена; попробуйте ещё раз.",
        })
    # Первый узел нового сегмента встал под ДРУГИМ id. Чтобы не переносить
    # все ссылки, забираем его содержимое в отложенный узел и убираем дубль.
    if new_id != node_id:
        state.story_tree[node_id] = state.story_tree.pop(new_id)
    for key in ("total_input_tokens", "total_output_tokens", "total_api_calls",
                "total_cache_hit_tokens", "total_cache_miss_tokens"):
        setattr(state, key, getattr(pending_state, key))
    return node_id


async def _build_tree_events(
    branch_state: StoryState, client: DeepSeekClient, tree: dict[str, dict],
    semaphore: asyncio.Semaphore, session_id: str, log_path: str | None,
    ceiling_scene: int, window: tuple[int, int] | None, origin_choice: str | None,
) -> str:
    """
    Строит СКЕЛЕТ участка дерева — только события (дёшево), не прозу.
    Возвращает id ПЕРВОГО узла сегмента — этот id стабилен и не меняется,
    когда сегмент позже материализуется в прозу (см. _materialize_segment):
    другие сегменты ссылаются на него через children/segment_children_ids
    независимо от того, дописана ли уже проза.

    origin_choice — явная метка вместо гадания по порядку завершения параллельных задач.

    window=None — финальный сегмент, без выбора, до конца истории.
    window=(start,end) — сегмент, который должен закончиться настоящим
    выбором где-то внутри окна. Защита контракта здесь та же, что была
    раньше, просто теперь проверяет pivot_choices из generate_segment_events,
    а не поле choices у уже написанной сцены — на этом этапе сцен ещё нет.
    """
    # Ограничитель должен охватывать только один сетевой планировочный блок.
    # Если держать его во время рекурсии, при TREE_CONCURRENCY=1 дочерние
    # ветки ждут слот, который удерживает их же родитель.
    events: list[str] = []
    raw_pivot: list[str] | None = None
    events_usage: dict = {}
    branch_state.pending_choice = origin_choice if origin_choice and not origin_choice.startswith("(") else None
    start_scene_requested = branch_state.current_scene + 1
    generation_id = uuid_lib.uuid4().hex
    for attempt in range(1, MAX_EVENT_GENERATION_ATTEMPTS + 1):
        try:
            events, raw_pivot, events_usage = await _limited_llm_call(semaphore, 
                client.generate_segment_events,
                branch_state, start_scene_requested, ceiling_scene, window,
                _attempt=attempt, _generation_id=generation_id,
            )
        except (json.JSONDecodeError, KeyError):
            continue
        except httpx.HTTPError:
            # Таймаут/сетевой сбой конкретно на этом сегменте — ретраим
            # ЕГО, не пробрасываем наверх к дорогому перезапуску всего
            # дерева через MAX_TREE_BUILD_ATTEMPTS.
            continue

        break
    events = through_ending(events)
    terminal = bool(events and terminal_event(events[-1]))
    if terminal:
        raw_pivot = None
    # Future branch plans never enter the accepted event history.

    _log_debug(session_id, log_path, "segment_events", {
        "origin_choice": origin_choice,
        "start_scene_requested": start_scene_requested,
        "segment_index": branch_state.segment_index,
        "branch_path": list(branch_state.branch_path),
        "ceiling_scene": ceiling_scene,
        "window": list(window) if window else None,
        "events": events,
        "raw_pivot_choices": raw_pivot,
        "usage": events_usage,
        "review_required": True,
    })
    
    status = TREE_BUILD_STATUS.get(session_id)
    
    if not events:
        # 3 попытки исчерпаны — не роняем всё дерево, узел становится
        # тихим финалом этой ветки (сразу материализованным — заглушке
        # не нужна лениво генерируемая проза).
        scene_dict = {
            "narration": "Здесь история обрывается — не удалось спланировать эту ветку.",
            "speaker": None, "dialogue": None, "location": branch_state.location,
            "choices": [], "ending": "failure",
        }
        node_id = uuid_lib.uuid4().hex
        tree[node_id] = {"scene": scene_dict, "children": {}, "materialized": True,
                         "generation_error": True}
        if status is not None:
            status["nodes_built"] = status.get("nodes_built", 0) + 1
        return node_id
    
    first_node_id = uuid_lib.uuid4().hex
    _configure_segment(tree, first_node_id, branch_state, events, raw_pivot, window, origin_choice)
    if status is not None:
        status["nodes_built"] = status.get("nodes_built", 0) + len(events)
    tree[first_node_id].update({
        "segment_index": branch_state.segment_index,
        "branch_path": list(branch_state.branch_path),
        "segment_review_status": "pending", "segment_window": list(window) if window else None,
        "segment_ceiling": ceiling_scene,
    })
    return first_node_id

def _configure_segment(tree, node_id, branch_state, events, raw_pivot, window, origin_choice):
    """Rebuild only deferred continuations after locally applied segment edits."""
    terminal = bool(events and terminal_event(events[-1]))
    # ЗАЩИТА КОНТРАКТА ОКНА — так же, как раньше для сцен, теперь для
    # событий: выбор, который лёг бы раньше начала окна, стирается;
    # финальный сегмент (window=None) никогда не получает выбор, что бы
    # ни вернула модель; больше 2 вариантов обрезается до 2.
    pivot_choices = _resolve_pivot_choices(raw_pivot, window, len(events), branch_state.current_scene)
    
    first_node_id = node_id
    
    if pivot_choices:
        # Развилка хранит два отложенных продолжения без глубокой генерации.
        opt_a, opt_b = pivot_choices[0], pivot_choices[1]
        state_a = copy.deepcopy(branch_state)
        state_b = copy.deepcopy(branch_state)
        # КРИТИЧНО: без явной записи текста выбора state_a и state_b были
        # бы АБСОЛЮТНО ИДЕНТИЧНЫ — модель, планирующая события ветки А,
        # не видела бы никакой разницы с веткой Б. "[Выбор героя]" — не
        # само событие, а факт того, что решение принято именно так,
        # чтобы дальнейшее планирование строилось от него, а не гадало.
        state_a.planned_events_so_far = branch_state.planned_events_so_far + [event_prompt(e) for e in events] + [f"[Выбор героя] {opt_a}"]
        state_b.planned_events_so_far = branch_state.planned_events_so_far + [event_prompt(e) for e in events] + [f"[Выбор героя] {opt_b}"]
        state_a.current_scene = branch_state.current_scene + len(events)
        state_b.current_scene = branch_state.current_scene + len(events)
        next_bounds = _segment_after(window, branch_state.target_length)
        next_ceiling, next_window = next_bounds if next_bounds else (branch_state.target_length, None)
    
        for child_state, choice in ((state_a, opt_a), (state_b, opt_b)):
            child_state.segment_index = branch_state.segment_index + 1
            child_state.pending_choice = choice
            child_state.branch_path = branch_state.branch_path + [choice]
        # Both modes store only placeholders here. Normal mode prepares one
        # immediate level after the current segment has been materialized.
        child_a_id = _make_pending_branch(tree, state_a, opt_a, next_ceiling, next_window)
        child_b_id = _make_pending_branch(tree, state_b, opt_b, next_ceiling, next_window)

        tree[first_node_id] = {
            "scene": None, "materialized": False,
            "segment_events": events, "segment_choice_texts": [opt_a, opt_b],
            "segment_children_ids": {opt_a: child_a_id, opt_b: child_b_id},
            "children": {}, "origin_choice": origin_choice,
        }
    else:
        already_finished = terminal or branch_state.current_scene + len(events) >= branch_state.target_length
        if window is not None and not already_finished:
            # Graceful-деградация на уровне событий — окно не дало
            # выбор, продолжаем линейно к следующей границе, не падаем.
            state_next = copy.deepcopy(branch_state)
            state_next.planned_events_so_far = branch_state.planned_events_so_far + [event_prompt(e) for e in events]
            state_next.current_scene = branch_state.current_scene + len(events)
            next_bounds = _segment_after(window, branch_state.target_length)
            next_ceiling, next_window = next_bounds if next_bounds else (branch_state.target_length, None)
            state_next.segment_index = branch_state.segment_index + 1
            state_next.pending_choice = None
            next_id = _make_pending_branch(
                tree, state_next, "(продолжение без развилки)", next_ceiling, next_window,
            )
            tree[first_node_id] = {
                "scene": None, "materialized": False,
                "segment_events": events, "segment_choice_texts": None,
                "segment_children_ids": {"Продолжить": next_id},
                "children": {}, "origin_choice": origin_choice,
            }
        else:
            # Финальный сегмент — конец истории, детей не будет.
            tree[first_node_id] = {
                "scene": None, "materialized": False,
                "segment_events": events, "segment_choice_texts": None,
                "segment_children_ids": {},
                "children": {}, "origin_choice": origin_choice,
            }
    
    return first_node_id


def _reconstruct_session(entries: list[dict]) -> tuple[StoryState, list[dict]]:
    """
    Восстанавливает StoryState и дерево из записей диагностического .jsonl —
    без единого обращения к модели: и события, и уже написанная проза (там,
    где она есть) просто переигрываются заново. Использует ТУ ЖЕ функцию
    защиты контракта (_resolve_pivot_choices), что и обычное построение —
    иначе реконструкция могла бы дать другой результат, чем сама генерация
    в своё время.

    Ветки, для которых в логе нет записи segment_prose (игрок либо не
    доходил до них, либо это была сторонняя ветка выбора) — остаются
    скелетом из событий, как и при обычном построении: материализуются
    лениво, если игрок реально выберет туда пойти (тогда уже нужен
    настоящий API-ключ, раз придётся писать новую прозу).

    Возвращает (state, resume_points) — resume_points перечисляет узел
    начала истории и КАЖДУЮ реально зафиксированную развилку (узел с
    настоящим выбором), чтобы игрок мог продолжить не только с начала.
    """
    plan_entry = next((e for e in entries if e.get("step") == "plan"), None)
    chars_entry = next((e for e in entries if e.get("step") == "characters"), None)
    if plan_entry is None or chars_entry is None:
        raise ValueError("В логе нет записи плана или каста — восстановить историю нельзя")

    events_entries = [e for e in entries if e.get("step") == "segment_events"]
    for review in (e for e in entries if e.get("step") == "segment_review"):
        for i, event_entry in enumerate(events_entries):
            if (event_entry.get('segment_index') == review.get('segment_index')
                    and event_entry.get('branch_path') == review.get('branch_path')):
                events_entries[i] = {**event_entry, 'events': review['events'],
                    'review_complete': True,
                    'raw_pivot_choices': (review['events'][-1].get('choices')
                        if isinstance(review['events'][-1], dict) else None)}
                break
    prose_entries = [e for e in entries if e.get("step") in {"segment_prose", "segment_prose_final"}]
    if not events_entries:
        raise ValueError("В логе нет ни одной записи событий сегмента")

    # New logs disambiguate identical event lists on different paths. Legacy
    # logs retain their exact-event matching when no path metadata exists.
    def prose_key(entry):
        source = tuple(event_prompt(ev) for ev in entry.get("events", []))
        if "branch_path" in entry:
            return source, tuple(entry["branch_path"]), entry.get("segment_index", 0)
        return source
    prose_by_events = {prose_key(e): e for e in prose_entries if e.get("raw_scenes")}

    # New incremental logs store the target explicitly; old complete-tree
    # logs can recover it from the deepest segment's ceiling.
    target_length = plan_entry.get("target_length") or max(
        (e.get("ceiling_scene", 0) for e in events_entries), default=40)

    state = StoryState(
        genre=plan_entry.get("genre", ""), tone=plan_entry.get("tone", ""),
        target_length=target_length,
    )
    state.story_plan = plan_entry.get("raw_plan")
    state.scene_conditions = initial_conditions(state.story_plan)
    state.build_all_branches = plan_entry.get("build_all_branches", True)
    if plan_entry.get("seed"):
        state.add_context(f"Завязка: {plan_entry['seed']}")
    for answer in plan_entry.get("clarifications", []):
        state.add_context(f"Уточнение: {answer}")

    imported_chars, imported_rels = prepare_cast(chars_entry.get("raw_characters", []), chars_entry.get("raw_relationships", []))
    for c in imported_chars:
        try:
            age = int(c.get("age"))
            state.add_character(
                str(c["name"]), age, str(c.get("role", "")),
                str(c.get("traits", "")), str(c.get("appearance", "")),
                story_role=str(c.get("story_role", "")), story_purpose=str(c.get("story_purpose", "")),
                is_protagonist=bool(c.get("is_protagonist", False)),
                appearance_features=c.get("appearance_features"), character_id=c.get("id"), kind=c.get("kind", "human"),
            )
        except (AgeValidationError, KeyError, TypeError, ValueError):
            # Неверный формат одного персонажа не должен ломать весь лог.
            # Числовой возраст ниже 18 нормализуется внутри Character.
            continue
    state.character_relationships = [r for r in imported_rels if all(cid in state.characters for cid in r["between"])]
    import hashlib
    state.sprite_seed = chars_entry.get("sprite_seed") or hashlib.sha256(
        json.dumps(imported_chars, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    saved_assignments = chars_entry.get("sprite_assignments", {})
    if isinstance(saved_assignments, dict):
        for cid, c in state.characters.items():
            saved = saved_assignments.get(cid, {})
            if isinstance(saved, dict) and isinstance(saved.get("sprite_id"), str):
                c.sprite_id = saved["sprite_id"]
                c.sprite_appearance = str(saved.get("sprite_appearance") or "")
    assign_sprites(state)


    tree: dict[str, dict] = {}
    resume_points: list[dict] = []
    events_pool = list(events_entries)  # использованные записи выбрасываем

    def build(origin_choice: str | None, branch_state: StoryState) -> str | None:
        idx = next((i for i, e in enumerate(events_pool)
                    if e.get("origin_choice") == origin_choice
                    and ("branch_path" not in e or e["branch_path"] == branch_state.branch_path)
                    and ("segment_index" not in e or e["segment_index"] == branch_state.segment_index)), None)
        if idx is None:
            return None
        entry = events_pool.pop(idx)
        events = through_ending(entry.get("events") or [])
        if not events:
            return None

        window = tuple(entry["window"]) if entry.get("window") else None
        scene_before = branch_state.current_scene
        pivot_choices = _resolve_pivot_choices(
            entry.get("raw_pivot_choices"), window, len(events), scene_before,
        )

        if terminal_event(events[-1]):
            pivot_choices = None

        source_key = tuple(event_prompt(ev) for ev in events)
        prose_entry = (prose_by_events.get((source_key, tuple(branch_state.branch_path),
                                            branch_state.segment_index))
                       or prose_by_events.get(prose_key(entry)))
        first_node_id = uuid_lib.uuid4().hex

        if prose_entry is None:
            # Не материализовано в прошлый раз — остаётся скелетом, как при
            # обычном построении. Дети (если пивот найден) тоже только
            # скелетом — рекурсия всё равно нужна, чтобы дерево было цельным.
            children_ids: dict[str, str] = {}
            if pivot_choices:
                opt_a, opt_b = pivot_choices
                state_a, state_b = copy.deepcopy(branch_state), copy.deepcopy(branch_state)
                # Та же правка, что и в _build_tree_events: без явной записи
                # текста выбора обе копии были бы идентичны.
                state_a.planned_events_so_far = branch_state.planned_events_so_far + [event_prompt(e) for e in events] + [f"[Выбор героя] {opt_a}"]
                state_b.planned_events_so_far = branch_state.planned_events_so_far + [event_prompt(e) for e in events] + [f"[Выбор героя] {opt_b}"]
                for s, choice in ((state_a, opt_a), (state_b, opt_b)):
                    s.current_scene = branch_state.current_scene + len(events)
                    s.segment_index = branch_state.segment_index + 1
                    s.pending_choice = choice
                    s.branch_path = branch_state.branch_path + [choice]
                child_a, child_b = build(opt_a, state_a), build(opt_b, state_b)
                next_bounds = _segment_after(window, branch_state.target_length)
                ceiling, next_window = next_bounds or (branch_state.target_length, None)
                child_a = child_a or _make_pending_branch(tree, state_a, opt_a, ceiling, next_window)
                child_b = child_b or _make_pending_branch(tree, state_b, opt_b, ceiling, next_window)
                children_ids = {opt_a: child_a, opt_b: child_b}
            if not pivot_choices and window is not None and not terminal_event(events[-1]):
                nxt = copy.deepcopy(branch_state)
                nxt.current_scene += len(events)
                nxt.segment_index += 1
                nxt.planned_events_so_far += [event_prompt(e) for e in events]
                ceiling, next_window = _segment_after(window, nxt.target_length) or (nxt.target_length, None)
                label = "(продолжение без развилки)"
                child = build(label, nxt) or _make_pending_branch(tree, nxt, label, ceiling, next_window)
                children_ids = {"Продолжить": child}
            tree[first_node_id] = {
                "scene": None, "materialized": False,
                "segment_events": events, "segment_choice_texts": pivot_choices,
                "segment_children_ids": children_ids, "children": {}, "origin_choice": origin_choice,
                "segment_index": branch_state.segment_index, "branch_path": list(branch_state.branch_path),
            }
            if entry.get('review_required'):
                tree[first_node_id].update({
                    'segment_review_status': 'complete' if entry.get('review_complete') else 'pending',
                    'segment_window': list(window) if window else None,
                    'segment_ceiling': entry.get('ceiling_scene', target_length)})
            return first_node_id

        # Проза уже была написана — реально применяем к branch_state
        # (факты/персонажи накапливаются по-настоящему), строим цепочку.
        raw_scenes = prose_entry.get("raw_scenes", [])
        if terminal_event(events[-1]):
            raw_scenes = raw_scenes[:len(events)]
        node_ids = [first_node_id] + [uuid_lib.uuid4().hex for _ in range(max(0, len(raw_scenes) - 1))]
        is_real_choice = origin_choice is not None and not origin_choice.startswith("(")
        applied = 0
        for i, raw in enumerate(raw_scenes):
            raw = normalize_prose(raw, branch_state.characters)
            history_start = len(branch_state.event_history)
            recorded = raw
            if i < len(events):
                raw = bind_event(raw, events[i])
            user_choice = origin_choice if (i == 0 and is_real_choice) else "Продолжить"
            try:
                location_fields = DeepSeekClient.apply_scene_to_state(branch_state, raw, user_choice,
                    legacy_locations=(prose_entry.get("location_schema") != 1
                                      and "location_id" not in raw and "location_transition" not in raw))
                pin = place_id(recorded.get("location_background_id"))
                if (prose_entry.get("step") == "segment_prose_final" and pin and pin != "abstract_space"
                        and location_fields["location_id"] == recorded.get("location_id")):
                    branch_state.locations[location_fields["location_id"]]["background_id"] = pin
                    location_fields["location_background_id"] = pin
            except AgeValidationError:
                break
            if prose_entry.get("step") == "segment_prose_final" and recorded.get("ending") == "complete":
                raw["ending"] = "complete"
            applied += 1
            is_last = (i == len(raw_scenes) - 1)
            scene_dict = {
                "narration": raw.get("narration", ""),
                **({"blocks": raw["blocks"]} if "blocks" in raw else {}),
                "dialogue": raw.get("dialogue"), **location_fields,
                "choices": list(pivot_choices) if (is_last and pivot_choices) else [],
                "ending": raw.get("ending") if raw.get("ending") in ("death", "failure", "victory", "complete") else None,
            }
            tree[node_ids[i]] = {"scene": scene_dict, "children": {},
                                "history_records": copy.deepcopy(branch_state.event_history[history_start:]),
                            "segment_index": branch_state.segment_index,
                            "branch_path": list(branch_state.branch_path)}
        if applied == 0:
            return None
        for i in range(applied - 1):
            tree[node_ids[i]]["children"] = {"Продолжить": node_ids[i + 1]}
        last_id = node_ids[applied - 1]
        tree[first_node_id]["segment_tail_id"] = last_id

        fully_applied = applied == len(raw_scenes) and prose_entry.get("segment_complete", True)
        if pivot_choices and fully_applied:
            # Точка возврата — САМ узел выбора (последний в сегменте), не
            # начало сегмента: "загрузиться в выбор" значит оказаться прямо
            # перед кнопками, не заново читать весь путь до них.
            resume_points.append({
                "node_id": last_id,
                "label": origin_choice or "Развилка после начала истории",
                "pivot_choices": pivot_choices,
            })
            opt_a, opt_b = pivot_choices
            state_a, state_b = copy.deepcopy(branch_state), copy.deepcopy(branch_state)
            state_a.planned_events_so_far = branch_state.planned_events_so_far + [event_prompt(e) for e in events] + [f"[Выбор героя] {opt_a}"]
            state_b.planned_events_so_far = branch_state.planned_events_so_far + [event_prompt(e) for e in events] + [f"[Выбор героя] {opt_b}"]
            for s, choice in ((state_a, opt_a), (state_b, opt_b)):
                s.segment_index = branch_state.segment_index + 1
                s.pending_choice = choice
                s.branch_path = branch_state.branch_path + [choice]
            child_a, child_b = build(opt_a, state_a), build(opt_b, state_b)
            next_bounds = _segment_after(window, branch_state.target_length)
            ceiling, next_window = next_bounds or (branch_state.target_length, None)
            child_a = child_a or _make_pending_branch(tree, state_a, opt_a, ceiling, next_window)
            child_b = child_b or _make_pending_branch(tree, state_b, opt_b, ceiling, next_window)
            tree[last_id]["children"] = {opt_a: child_a, opt_b: child_b}
        elif fully_applied and window is not None and not terminal_event(events[-1]):
            nxt = copy.deepcopy(branch_state)
            nxt.segment_index += 1
            nxt.planned_events_so_far += [event_prompt(e) for e in events]
            ceiling, next_window = _segment_after(window, nxt.target_length) or (nxt.target_length, None)
            label = "(продолжение без развилки)"
            child = build(label, nxt) or _make_pending_branch(tree, nxt, label, ceiling, next_window)
            tree[last_id]["children"] = {"Продолжить": child}

        return first_node_id

    root_id = build(None, state)
    if root_id is None:
        raise ValueError("Не удалось найти или переиграть корневой сегмент в логе")

    resume_points.insert(0, {"node_id": root_id, "label": "Начало истории", "pivot_choices": None})
    state.story_tree = tree
    state.tree_root_id = root_id
    state.tree_current_id = root_id
    state.tree_first_delivered = False

    return state, resume_points


def _mark_dead_end(node: dict, location: str, reason: str) -> None:
    """
    Превращает узел в тихий финал ветки: проза не появилась, предрешённые
    дети остаются недостижимыми. Вынесено из двух мест, где блок стоял
    дословно одинаковый — при правке одного второе легко забыть.

    reason различает причины, потому что для игрока это разные ситуации:
    сеть подвела (можно попробовать снова) или не удалось применить ответ
    (пробовать бессмысленно, нужен другой сюжетный ход).
    """
    node["scene"] = {
        "narration": f"Здесь история обрывается — {reason}",
        "speaker": None, "dialogue": None, "location": location,
        "choices": [], "ending": "failure",
    }
    node["children"] = {}
    node["materialized"] = True


async def _review_pending_segment(session_id, log_path, state, node_id, tree, client):
    """Review only new, unplayed drafts. Save edits before the prose call."""
    node = tree[node_id]
    draft = copy.deepcopy(node["segment_events"])
    choices = node.get("segment_choice_texts")
    if choices and isinstance(draft[-1], dict):
        draft[-1]['choices'] = list(choices)
    window = tuple(node['segment_window']) if node.get('segment_window') else None
    final = window is None or bool(draft and terminal_event(draft[-1]))
    generation_id = uuid_lib.uuid4().hex
    last_error = None
    for attempt in range(1, 3):
        try:
            corrected, replacements, usage = await _llm_call(
                client.review_segment_events, state, draft, final=final,
                _attempt=attempt, _generation_id=generation_id)
            break
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc
            _log_debug(session_id, log_path, "segment_review_failure", {
                "node_id": node_id, "attempt": attempt, "error_type": type(exc).__name__,
                "error": str(exc)[:500], "segment_index": state.segment_index,
                "branch_path": list(state.branch_path)})
    else:
        _save_snapshot(session_id, state)
        raise HTTPException(status_code=502, detail={
            "reason": "segment_review_failed", "message":
            "Не удалось проверить события. Сегмент сохранён; повторите продолжение."}) from last_error
    raw_pivot = corrected[-1].get('choices') if isinstance(corrected[-1], dict) else None
    original = copy.deepcopy(node)
    _configure_segment(tree, node_id, state, corrected, raw_pivot, window, node.get('origin_choice'))
    # These descendants are still deferred: no prose or paid plans are discarded.
    for child_id in original.get('segment_children_ids', {}).values():
        if tree.get(child_id, {}).get('pending_branch'):
            tree.pop(child_id, None)
    tree[node_id].update({key: original[key] for key in (
        'custom_action', 'segment_canon', 'segment_window', 'segment_ceiling',
        'segment_index', 'branch_path') if key in original})
    tree[node_id]['segment_review_status'] = 'complete'
    tree[node_id]['segment_review'] = {'replacements': replacements, 'usage': usage}
    _log_debug(session_id, log_path, 'segment_review', {
        'node_id': node_id, 'segment_index': state.segment_index, 'branch_path': list(state.branch_path),
        'original_events': draft, 'events': corrected, 'replacements': replacements,
        'usage': usage, 'final': final, 'attempts_used': attempt})
    _save_snapshot(session_id, state)


async def _materialize_segment(
    session_id: str, log_path: str | None, branch_state: StoryState,
    node_id: str, tree: dict[str, dict], client: DeepSeekClient,
) -> None:
    """
    Дорогой этап — вызывается ЛЕНИВО из /story/scene, только когда игрок
    реально дошёл до этого сегмента. events/choice_texts/children_ids уже
    решены заранее на этапе построения скелета (_build_tree_events) —
    здесь только пишется проза и мутируется СУЩЕСТВУЮЩИЙ узел node_id
    (не создаётся новый — другие сегменты уже ссылаются на этот id).

    branch_state должен на входе отражать РЕАЛЬНЫЕ накопленные факты вплоть
    до этого сегмента (проза для всех более ранних сегментов этой ветки
    уже написана и применена — иначе to_prompt_block() внутри
    generate_segment_prose был бы пуст там, где не должен).
    """
    node = tree[node_id]
    restore_location_path(branch_state, tree, node_id)
    for fact in node.get("segment_canon", []):
        branch_state.add_context(fact)
    branch_state.segment_index = node.get("segment_index", branch_state.segment_index)
    branch_state.branch_path = list(node.get("branch_path", branch_state.branch_path))
    branch_state.pending_choice = node.get("origin_choice")
    if branch_state.pending_choice and branch_state.pending_choice.startswith("("):
        branch_state.pending_choice = None
    if node.get("segment_review_status") == "pending":
        await _review_pending_segment(session_id, log_path, branch_state, node_id, tree, client)
        node = tree[node_id]
    events: list[str] = node["segment_events"]
    choice_texts = node["segment_choice_texts"]
    children_ids: dict = node["segment_children_ids"]
    origin_choice: str | None = node.get("origin_choice")
    raw_scenes: list[dict] | None = None
    prose_usage: dict = {}
    generation_id = uuid_lib.uuid4().hex
    for attempt in range(1, 4):
        try:
            candidate_scenes, prose_usage = await _llm_call(
                client.generate_segment_prose,
                branch_state, events, tuple(choice_texts) if choice_texts else None,
                _attempt=attempt, _generation_id=generation_id,
            )
            raw_scenes = candidate_scenes
            break
        except (json.JSONDecodeError, KeyError):
            continue
        except httpx.HTTPError:
            continue
        except Exception as e:
            # Неожиданный тип сбоя (не HTTP/JSON/Key) раньше пробивался мимо
            # ретраев и валил материализацию целиком, оставляя узел-тупик.
            # Считаем его таким же транзиентным, как сетевой сбой: повторяем,
            # а если не выйдет за 3 попытки — ниже сработает _mark_dead_end.
            _log_debug(session_id, log_path, "prose_call_failure", {
                "node_id": node_id,
                "error_type": type(e).__name__,
                "error": str(e)[:500],
            })
            continue

    _log_debug(session_id, log_path, "segment_prose", {
        "location_schema": 1,
        "node_id": node_id,
        "segment_index": branch_state.segment_index,
        "branch_path": list(branch_state.branch_path),
        "events": events,
        "choice_texts": choice_texts,
        "raw_scenes": raw_scenes,
        "usage": prose_usage,
    })

    if not raw_scenes:
        _save_snapshot(session_id, branch_state)
        raise HTTPException(status_code=502, detail={
            "reason": "generation_failed",
            "message": "Не удалось получить текст продолжения. События сохранены; попробуйте ещё раз.",
        })

    # Принимаем полученный текст без редактора, сюжетных проб и исправлений.
    # Плановые результаты и условия привязываются только к существующим сценам.
    scenes_to_apply = (raw_scenes[:len(events)]
                       if events and terminal_event(events[-1]) else raw_scenes)
    applied_scene_dicts = [
        bind_event(scene, events[i]) if i < len(events) else dict(scene)
        for i, scene in enumerate(scenes_to_apply)
    ]
    stopped_reason = None

    # Теперь итоговый набор сцен зафиксирован — можно раздать им id.
    # Первый обязательно node_id: на него уже ссылаются другие сегменты.
    node_ids: list[str] = [node_id]
    while len(node_ids) < len(applied_scene_dicts):
        node_ids.append(uuid_lib.uuid4().hex)

    # Только ПЕРВАЯ сцена сегмента реально следует за выбором — записываем
    # туда настоящий текст, а не служебное "Продолжить". Раньше история
    # выборов в state НИКОГДА не содержала того, что игрок/ветка реально
    # выбрали — только эту заглушку, из-за чего модель не могла отличить
    # ветку А от ветки Б по накопленному состоянию.
    is_real_choice = origin_choice is not None and not origin_choice.startswith("(")

    applied_count = 0
    for i, raw in enumerate(applied_scene_dicts):
        raw = normalize_prose(raw, branch_state.characters)
        history_start = len(branch_state.event_history)
        user_choice = origin_choice if (i == 0 and is_real_choice) else "Продолжить"
        try:
            candidate_state = copy.deepcopy(branch_state)
            location_fields = DeepSeekClient.apply_scene_to_state(candidate_state, raw, user_choice)
            for key, value in vars(candidate_state).items():
                if key != "story_tree":
                    setattr(branch_state, key, value)
        except AgeValidationError as e:
            stopped_reason = f"age_validation: {e}"
            break
        except Exception as e:
            # Ошибка локального применения состояния не должна валить всю
            # материализацию: фиксируем обрыв и дальше сработает страховка
            # на ending, чтобы ветка закрылась, а не стала тупиком.
            stopped_reason = f"apply_state_failure: {type(e).__name__}"
            break
        applied_count += 1

        is_last_planned = (i == len(applied_scene_dicts) - 1)
        narration_text = raw.get("narration", "") or ""
        dialogue_text = raw.get("dialogue")
        # Дедупликация narration/dialogue. Модель регулярно вписывает реплику
        # И в прозу («— говорит он…»), И в отдельное поле dialogue — тогда
        # фронт показывает её дважды: сначала абзацем, потом отдельной
        # строкой. Если текст реплики уже дословно есть в narration,
        # отдельное поле не нужно: реплика останется в прозе один раз.
        if dialogue_text and dialogue_text.strip() and dialogue_text.strip() in narration_text:
            dialogue_text = None
        scene_dict = {
            "narration": narration_text,
            **({"blocks": raw["blocks"]} if "blocks" in raw else {}),
            **location_fields,
            "speaker_id": location_fields.get("speaker_id") if dialogue_text else None,
            "speaker": location_fields.get("speaker") if dialogue_text else None,
            "dialogue": dialogue_text,
            # Выбор на последней сцене — ТОЛЬКО предрешённый (choice_texts),
            # никогда не то, что вернула модель в поле choices у самой
            # прозы: структура дерева (кто дети) уже зафиксирована на этапе
            # событий, проза не может её поменять, только одеть в текст.
            "choices": list(choice_texts) if (is_last_planned and choice_texts) else [],
            "ending": raw.get("ending") if raw.get("ending") in ("death", "failure", "victory", "complete") else None,
        }
        tree[node_ids[i]] = {"scene": scene_dict, "children": {},
                            "history_records": copy.deepcopy(branch_state.event_history[history_start:]),
                            "segment_index": branch_state.segment_index,
                            "branch_path": list(branch_state.branch_path)}

    _log_debug(session_id, log_path, "segment_application", {
        "node_id": node_id,
        "planned_events": len(events),
        "generated_scenes": len(applied_scene_dicts),
        "applied_scenes": applied_count,
        "stopped_reason": stopped_reason,
    })

    if applied_count == 0:
        _mark_dead_end(node, branch_state.location, "не удалось применить полученные сцены.")
        return

    # Связываем цепочку узлов друг с другом
    for i in range(applied_count - 1):
        tree[node_ids[i]]["children"] = {"Продолжить": node_ids[i + 1]}

    last_id = node_ids[applied_count - 1]
    tree[node_ids[0]]["segment_tail_id"] = last_id
    if node.get('custom_action'):
        tree[node_ids[0]]['custom_action'] = True
    if stopped_reason is None and applied_count == len(applied_scene_dicts):
        # Весь полученный текст применён без сбоя — последняя
        # сцена получает предрешённых детей (choice_texts/children_ids),
        # как и было решено на этапе построения скелета.
        if choice_texts and children_ids:
            tree[last_id]["children"] = dict(zip(choice_texts, [children_ids[c] for c in choice_texts]))
        elif children_ids:
            tree[last_id]["children"] = dict(children_ids)
    if stopped_reason or applied_count < len(applied_scene_dicts):
        tree[last_id]["generation_stopped"] = True
        tree[last_id]["children"] = {}
        tree[last_id]["scene"]["choices"] = []
        tree[last_id]["scene"]["ending"] = "failure"

    # Страховка на ending: если после всего у последней сцены нет детей —
    # это конец истории, и поле обязано быть заполнено. Промпт об этом
    # просит, но на реальном прогоне модель написала убедительную развязку
    # и всё равно оставила ending=null, а фронт по этому полю решает,
    # показывать финальный экран или нет. Ставим НЕЙТРАЛЬНОЕ "complete":
    # мы знаем, что история кончилась, но не знаем чем именно — навязывать
    # "failure" было бы враньём (фронт показал бы "Провал" на истории с
    # хорошим концом), а "victory" наугад — тем более.
    final_scene = tree[last_id].get("scene")
    if final_scene is not None and not tree[last_id].get("children") and not final_scene.get("ending"):
        # Если ветка оборвалась из-за сбоя (а не просто кончились события) —
        # это честнее показать как "failure", чем как нейтральный "complete":
        # игрок должен понимать, что история прервалась, а не завершилась.
        # Если же события кончились нормально — "complete" (чем именно
        # кончилось, мы не знаем, навязывать "victory" наугад нельзя).
        if stopped_reason is not None:
            final_scene["ending"] = "failure"
        else:
            final_scene["ending"] = "complete"

    # Keep applied metadata and generated prose for faithful JSONL replay;
    # the earlier segment_prose entry intentionally contains the raw response.
    _log_debug(session_id, log_path, "segment_prose_final", {
        "location_schema": 1, "node_id": node_id, "events": events,
        "segment_index": branch_state.segment_index,
        "branch_path": list(branch_state.branch_path),
        "segment_complete": stopped_reason is None and applied_count == len(applied_scene_dicts),
        "raw_scenes": [{**applied_scene_dicts[i], **tree[node_ids[i]]["scene"]}
                       for i in range(applied_count)],
    })


async def _build_tree_background(session_id: str, root_state: StoryState, req: "StartRequest") -> None:
    """
    Фоновая оркестрация всего построения: каст -> дерево -> сохранение.
    Не бросает исключения наружу — они некому было бы ловить (задача не
    awaited из HTTP-обработчика) — вместо этого пишет ошибку в
    TREE_BUILD_STATUS, чтобы фронт при опросе получил понятный отказ,
    а не тихо зависший прогресс навечно. Если попытка целиком не удалась
    (сеть легла посреди построения, любая необработанная ошибка) — не
    откатываемся на какой-то другой, непротестированный режим генерации.
    Вместо этого повторяем ВСЮ попытку с чистого листа, до
    MAX_TREE_BUILD_ATTEMPTS раз, и только если ни одна не удалась —
    сообщаем честную ошибку.
    """
    TREE_BUILD_STATUS[session_id] = {"done": False, "error": None}
    _forget_old_builds()

    # Снимок состояния ДО каких-либо мутаций этой функции (факты из затравки
    # уже есть, персонажей и дерева ещё нет). Каждая попытка стартует с его
    # чистой копии — иначе частичные изменения от неудачной попытки (часть
    # персонажей уже добавлена, дерево наполовину построено) просочились бы
    # в следующую попытку вместо честного старта с нуля.
    base_snapshot = copy.deepcopy(root_state)
    last_error: Exception | None = None

    for attempt in range(MAX_TREE_BUILD_ATTEMPTS):
        try:
            work_state = copy.deepcopy(base_snapshot)
            client = _client(session_id=session_id)

            # Сброс на каждой попытке — счётчик от предыдущей неудачной
            # попытки не должен протечь в следующую, иначе прогресс на
            # экране выглядел бы так, будто уже построено больше, чем
            # реально есть в этой, свежей, попытке.
            TREE_BUILD_STATUS[session_id]["nodes_built"] = 0

            count_min, count_max = cast_size_range(work_state.target_length)
            raw_chars = raw_rels = None
            chars_usage = {}
            generation_id = uuid_lib.uuid4().hex
            for cast_attempt in range(1, 4):
                try:
                    raw_chars, raw_rels, chars_usage = await _llm_call(
                        client.generate_characters,
                        req.seed, req.genre, req.tone, work_state.story_plan,
                        count_min, count_max, work_state,
                        _attempt=cast_attempt, _generation_id=generation_id,
                    )
                    break
                except httpx.HTTPError:
                    # Таймаут/сетевой сбой — ретраим здесь же, не пробрасываем
                    # наверх: на этой стадии ещё ничего дорогого не построено
                    # (дерево начнётся только после каста), но всё равно
                    # дешевле повторить один вызов, чем откатывать всю
                    # попытку через внешний MAX_TREE_BUILD_ATTEMPTS.
                    continue
            if raw_chars is None:
                raise httpx.HTTPError("генерация каста не удалась за 3 попытки")

            raw_chars, raw_rels = prepare_cast(raw_chars, raw_rels)
            added_ids: set[str] = set()
            for c in raw_chars:
                try:
                    age = int(c.get("age"))
                    added = work_state.add_character(
                        str(c["name"]), age, str(c.get("role", "")),
                        str(c.get("traits", "")), str(c.get("appearance", "")),
                        story_role=str(c.get("story_role", "")), story_purpose=str(c.get("story_purpose", "")),
                        is_protagonist=bool(c.get("is_protagonist", False)),
                        appearance_features=c.get("appearance_features"), character_id=c.get("id"), kind=c.get("kind", "human"),
                    )
                    added_ids.add(added.id)
                except (AgeValidationError, TypeError, ValueError, KeyError):
                    continue  # один невалидный персонаж не должен ронять весь каст

            # ЗАЩИТА: модель могла пометить 0 протагонистов (забыла) или
            # несколько (не поняла, что должен быть ровно один) — код не
            # доверяет этому вслепую. Если после цикла протагонист не ровно
            # один — оставляем ПЕРВОГО помеченного (если был хоть один),
            # иначе первого добавленного персонажа вообще.
            protagonists = [c for c in work_state.characters.values() if c.is_protagonist]
            if len(protagonists) != 1:
                for c in work_state.characters.values():
                    c.is_protagonist = False
                fallback = protagonists[0] if protagonists else next(iter(work_state.characters.values()), None)
                if fallback is not None:
                    fallback.is_protagonist = True

            # Связь, где хотя бы один из двоих не пережил валидацию возраста
            # (отклонён выше), сама по себе становится бессмысленной — держать
            # её значило бы ссылаться на персонажа, которого нет в state.characters.
            work_state.character_relationships = [
                r for r in raw_rels
                if r["between"][0] in added_ids and r["between"][1] in added_ids
            ]

            assign_sprites(work_state)
            _log_debug(session_id, req.log_path, "characters", {
                "raw_characters": raw_chars, "raw_relationships": raw_rels, "usage": chars_usage,
                "sprite_seed": work_state.sprite_seed,
                "sprite_assignments": {cid: {"sprite_id": c.sprite_id, "sprite_appearance": c.sprite_appearance}
                                       for cid, c in work_state.characters.items()},
            })
            tree: dict[str, dict] = {}
            semaphore = asyncio.Semaphore(TREE_CONCURRENCY)
            first_window = choice_windows(work_state.target_length)[0]
            root_id = await _build_tree_events(
                work_state, client, tree, semaphore, session_id, req.log_path,
                first_window[1], first_window, None,
            )

            work_state.story_tree = tree
            # Корень не построился (сеть легла на первом же вызове) — дерева
            # нет вообще. Без этой проверки статус сказал бы «готово», игрок
            # нажал бы «играть» и упёрся в отказ уже внутри игры. Честнее
            # сообщить об ошибке здесь: экран построения предложит начать
            # заново, а не создаст видимость успеха.
            if not root_id or not tree:
                raise RuntimeError("не удалось построить ни одного сегмента истории")

            work_state.tree_root_id = root_id
            work_state.tree_current_id = root_id

            # Успех — переносим результат в ТОТ ЖЕ объект, что уже
            # зарегистрирован в SESSIONS (нельзя просто заменить root_state
            # новым объектом — identity должна сохраниться для остального кода).
            root_state.__dict__.update(work_state.__dict__)
            # Дерево построено — самая дорогая часть истории уже оплачена.
            # Снимок прямо здесь, до всякой игры.
            _save_snapshot(session_id, root_state)
            TREE_BUILD_STATUS[session_id] = {
                "done": True,
                "error": None,
                "node_count": len(tree),
                "attempts_used": attempt + 1,
            }
            return
        except Exception as e:  # noqa: BLE001 — любая ошибка попытки должна дойти сюда, не потеряться
            last_error = e
            continue  # пробуем снова с чистого листа, если попытки остались

    TREE_BUILD_STATUS[session_id] = {"done": True, "error": str(last_error)}


@app.get("/story/tree_status/{session_id}")
async def tree_status(session_id: str):
    status = TREE_BUILD_STATUS.get(session_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Построение дерева для этой сессии не запускалось")
    return status


@app.post("/story/load_jsonl")
async def load_jsonl(file: UploadFile = File(...)):
    """
    Для продолжения истории из диагностического лога восстанавливает
    сессию из диагностического .jsonl, который игрок сам выгружал по ходу
    прошлой игры (см. log_path в StartRequest) — без единого обращения
    к модели для уже записанных сегментов.
    """
    try:
        content = (await file.read()).decode("utf-8-sig")
        entries = [json.loads(line) for line in content.splitlines() if line.strip()]
        if not entries or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("Каждая строка диагностического лога должна быть JSON-объектом")
        state, resume_points = _reconstruct_session(entries)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(
            status_code=422,
            detail={"reason": "bad_jsonl", "message": f"Файл повреждён или не в формате .jsonl: {e}"},
        )
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        # Чаще всего сюда приходит транскрипт вместо диагностического лога:
        # он тоже .jsonl, но содержит только тексты сцен, без плана и каста,
        # и для реконструкции недостаточен. Поясняем это прямо, иначе игрок
        # видит сухое «нет плана или каста» и не понимает, что не так с файлом.
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "incomplete_log",
                "message": (
                    f"{e}. Для восстановления нужен ДИАГНОСТИЧЕСКИЙ лог "
                    f"(*_debug.jsonl) — с планом, кастом и событиями сцен. "
                    f"Игровой протокол истории (*_transcript.jsonl) не подходит."
                ),
            },
        )

    session_id = str(uuid_lib.uuid4())
    _remember_session(session_id, state)
    return {
        "session_id": session_id,
        "resume_points": resume_points,
        "genre": state.genre,
        "tone": state.tone,
        "target_length": state.target_length,
    }


class ResumeRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    node_id: str = Field(min_length=1, max_length=64)


async def _resume_at_impl(req: ResumeRequest):
    """
    Устанавливает точку входа в уже восстановленной через /story/load_jsonl
    сессии и отдаёт первую сцену оттуда — с начала истории или прямо с
    зафиксированной развилки. Материализует прозу лениво, если точка ведёт
    в скелет без текста (сторона выбора, которую в прошлый раз не выбрали).
    """
    state = _get_state(req.session_id)
    if req.node_id not in state.story_tree:
        raise HTTPException(status_code=404, detail="Такой точки нет в восстановленном дереве")

    node = await _ensure_materialized(req.session_id, state, req.node_id)
    # Ставим курсор доставленным только после успешной материализации:
    # при сбое повторный запрос должен ещё раз попытаться получить сцену.
    state.tree_current_id = req.node_id
    state.tree_first_delivered = True
    scene_dict = node["scene"]
    finished = not node.get("children")
    state.delivered_scenes = state.current_scene
    _save_snapshot(req.session_id, state)
    return {
        "scene": _scene_with_visuals(state, scene_dict, session_id=req.session_id),
        "state": _public_state(state, reveal_all=finished),
        "finished": finished,

    }


@app.post("/story/start")
async def start_story(req: StartRequest):
    session_id = str(uuid_lib.uuid4())
    state = StoryState(
        genre=req.genre,
        tone=req.tone,
        target_length=req.target_length,
    )
    state.log_path = req.log_path
    state.build_all_branches = req.build_all_branches
    state.add_context(f"Завязка: {req.seed}")
    for answer in req.clarifications:
        state.add_context(f"Уточнение: {answer}")

    # План — разовый вызов, не критичный для старта: если он не удался
    # (сеть, битый JSON), история всё равно должна начаться. Реактивная
    # генерация сцен просто пойдёт без ориентира на план (build_segment_plan_directive
    # корректно обрабатывает story_plan=None).
    try:
        client = _client(session_id=session_id)
        raw_plan = None
        plan_usage = {}
        generation_id = uuid_lib.uuid4().hex
        for attempt in range(1, 4):
            try:
                raw_plan, plan_usage = await _llm_call(
                    client.generate_story_plan,
                    req.seed, req.genre, req.tone,
                    req.target_length, req.clarifications, state,
                    _attempt=attempt, _generation_id=generation_id,
                )
                break
            except httpx.HTTPError:
                # Таймаут/сетевой сбой — транзиентная проблема, повторяем
                # тот же запрос. Битый JSON (ловится ниже, отдельно) —
                # не повторяем, это не пройдёт и со второй попытки просто
                # так же, содержательно менять запрос здесь нечем.
                continue
        if raw_plan is None:
            raise httpx.HTTPError("генерация плана не удалась за 3 попытки")

        _log_debug(session_id, req.log_path, "plan", {
            "seed": req.seed, "genre": req.genre, "tone": req.tone,
            "raw_plan": raw_plan, "usage": plan_usage, "build_all_branches": req.build_all_branches,
            "target_length": req.target_length,
            "clarifications": req.clarifications,
        })
        state.story_plan = raw_plan
        state.scene_conditions = initial_conditions(raw_plan)
    except (httpx.HTTPError, json.JSONDecodeError, KeyError):
        pass  # история начнётся без плана — не блокируем старт из-за этого

    _remember_session(session_id, state)

    # Дерево строится в фоне — 1.5-2.3x базовой длины сцен, минуты, а не
    # секунды. Блокировать на это HTTP-ответ означало бы либо таймаут
    # клиента, либо очень долгий "зависший" запрос — вместо этого фронт
    # получает session_id сразу и опрашивает /story/tree_status, показывая
    # экран ожидания, пока дерево не будет готово.
    task = asyncio.create_task(_build_tree_background(session_id, state, req))
    TREE_BUILD_TASKS[session_id] = task

    return {
        "session_id": session_id,
        "state": _public_state(state),
    }


async def _ensure_materialized(session_id: str, state: StoryState, node_id: str) -> dict:
    """
    Возвращает АКТУАЛЬНЫЙ узел — материализует прозу, если она ещё не
    написана (node["scene"] is None). Обязательно перечитывает узел из
    дерева ПОСЛЕ материализации, а не переиспользует ссылку, полученную до
    неё: _materialize_segment при успехе ПЕРЕПРИСВАИВАЕТ запись в дереве
    (создаёт новый dict), а не мутирует старый на месте — старая ссылка
    осталась бы указывать на скелет с scene=None, даже если материализация
    прошла успешно.

    Клиент создаётся только если материализация реально нужна — чистая
    навигация по уже написанному не должна требовать ключ API вообще.
    """
    node = state.story_tree[node_id]
    if node.get("pending_branch"):
        await _build_pending_branch(session_id, state, node_id)
        node = state.story_tree[node_id]
        _save_snapshot(session_id, state)
    if node.get("scene") is None:
        restore_event_path(state, state.story_tree, node_id)
        client = _client(session_id=session_id)
        await _materialize_segment(session_id, state.log_path, state, node_id, state.story_tree, client)
        node = state.story_tree[node_id]  # обязательный перечит после материализации
        # Проза только что написана и оплачена — закрепляем на диске сразу,
        # чтобы падение бэкенда не заставило генерировать её повторно.
        _save_snapshot(session_id, state)
    await _prepare_segment_alternatives(session_id, state, node_id)
    restore_event_path(state, state.story_tree, node_id, include_current=True)
    state.segment_index = node.get("segment_index", state.segment_index)
    state.branch_path = list(node.get("branch_path", state.branch_path))
    state.pending_choice = None
    return node


async def _prepare_segment_alternatives(session_id: str, state: StoryState, node_id: str) -> None:
    """Prepare exactly one level of events, never prose or grandchildren."""
    if not state.build_all_branches:
        return
    first = state.story_tree[node_id]
    tail_id = first.get("segment_tail_id", node_id)
    tail = state.story_tree[tail_id]
    choices = (tail.get("scene") or {}).get("choices") or []
    if len(choices) != 2:
        return
    # Use a copy at the future pivot. Do not expose unshown scenes as accepted
    # history in the player's live state, or share mutable sibling snapshots.
    for choice in choices:
        child_id = tail.get("children", {}).get(choice)
        if child_id and state.story_tree.get(child_id, {}).get("pending_branch"):
            fork = copy.deepcopy(state)
            fork.story_tree = state.story_tree
            try:
                await _build_pending_branch(session_id, fork, child_id)
            except HTTPException as error:
                if error.status_code != 502:
                    raise
                _log_debug(session_id, state.log_path, "alternative_deferred", {"node_id": child_id})
            for key in ("total_input_tokens", "total_output_tokens", "total_api_calls",
                        "total_cache_hit_tokens", "total_cache_miss_tokens"):
                setattr(state, key, getattr(fork, key))
            # Persist each paid alternative immediately; resuming reuses it.
            _save_snapshot(session_id, state)


class CustomChoiceRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    custom_text: str = Field(min_length=1, max_length=500)
    proposal_id: str | None = Field(default=None, min_length=1, max_length=64)
    from_node_id: str | None = Field(default=None, min_length=1, max_length=64)


async def _custom_choice_impl(req: CustomChoiceRequest):
    """Проверяет свободное действие или продолжает по подтверждённому исправлению.

    Отклонённый ход не изменяет сюжет: игрок сначала выбирает один из трёх.
    """
    state = _get_state(req.session_id)
    _check_scene_position(state, req.from_node_id)
    if not state.build_all_branches:
        raise HTTPException(status_code=409, detail={
            "reason": "custom_choice_unavailable",
            "message": "Своё действие доступно в обычном режиме с подготовкой двух ближайших продолжений.",
        })
    if not state.tree_current_id or not state.story_tree:
        raise HTTPException(status_code=422, detail="Для этой сессии история не построена")

    node = state.story_tree.get(state.tree_current_id)
    if node is None:
        raise HTTPException(status_code=500, detail="Повреждённое состояние дерева")

    children = node.get("children", {})
    if len(children) < 2:
        raise HTTPException(
            status_code=409,
            detail={"reason": "not_a_choice", "message": "Здесь нет развилки — свой вариант некуда вписать."},
        )

    restore_event_path(state, state.story_tree, state.tree_current_id, include_current=True)
    custom_text = req.custom_text.strip()
    if not custom_text:
        raise HTTPException(status_code=422, detail="Действие не может быть пустым")
    cached_id = children.get(custom_text)
    if cached_id and state.story_tree.get(cached_id, {}).get('custom_action'):
        return await _deliver_custom_branch(req, state, node, custom_text, cached_id)
    proposal = node.get("custom_choice_proposal")
    if req.proposal_id and (
        not isinstance(proposal, dict) or proposal.get("id") != req.proposal_id
        or custom_text not in proposal.get("options", [])
    ):
        raise HTTPException(status_code=409, detail={
            "message": "Уточнение устарело. Предложите действие на текущей развилке заново.",
        })

    client = _client(session_id=req.session_id)
    try:
        if req.proposal_id:
            # Три исправления уже оценены моделью. Подтверждение игрока
            # запускает планирование, без ещё одного запроса на проверку.
            events, usage = await _llm_call(client.generate_custom_choice_events, state, custom_text)
        else:
            options = list(children)[:2]
            branch_events = [state.story_tree.get(children[o], {}).get("segment_events", [])
                             for o in options]
            decision, usage = await _llm_call(
                client.validate_custom_choice, state, options[0], options[1],
                branch_events[0], branch_events[1], custom_text,
            )
            if not decision["accepted"]:
                proposal_id = uuid_lib.uuid4().hex
                node["custom_choice_proposal"] = {
                    "id": proposal_id, "original_text": custom_text,
                    "options": decision["clarifying_options"],
                }
                _save_snapshot(req.session_id, state)
                _log_debug(req.session_id, state.log_path, "custom_choice", {
                    "custom_text": custom_text, "accepted": False,
                    "reason": decision["reason"],
                    "clarifying_question": decision["clarifying_question"],
                    "clarifying_options": decision["clarifying_options"], "usage": usage,
                })
                return {"accepted": False, "reason": decision["reason"],
                        "clarifying_question": decision["clarifying_question"],
                        "clarifying_options": decision["clarifying_options"],
                        "proposal_id": proposal_id}
            events = decision["events"]
    except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
        raise _api_failure(e, "проверка и продолжение своего варианта")

    _log_debug(req.session_id, state.log_path, "custom_choice", {
        "custom_text": custom_text, "accepted": True,
        "confirmed_proposal": bool(req.proposal_id),
        "original_text": proposal.get("original_text") if req.proposal_id else custom_text,
        "resulting_events": events, "usage": usage,
    })

    # Existing custom-action contract: validated events lead to a finale.
    # Ordinary choices continue through the incremental segment outline.
    new_node_id = uuid_lib.uuid4().hex
    state.story_tree[new_node_id] = {
        "scene": None, "materialized": False,
        "custom_action": True,
        "segment_review_status": "pending", "segment_window": None,
        "segment_ceiling": state.target_length,
        "origin_choice": custom_text,
        "segment_index": max(0, len((state.story_plan or {}).get("segments", [])) - 1),
        "branch_path": state.branch_path + [custom_text],
        "segment_events": events,
        "segment_choice_texts": None,
        "segment_children_ids": {},
        "children": {},
    }
    node["children"][custom_text] = new_node_id
    _save_snapshot(req.session_id, state)
    return await _deliver_custom_branch(req, state, node, custom_text, new_node_id)


async def _deliver_custom_branch(req, state, pivot, custom_text, new_node_id):
    """Commit the cursor only after a paid custom branch is available."""

    materialized = await _ensure_materialized(req.session_id, state, new_node_id)
    pivot.pop("custom_choice_proposal", None)
    state.tree_current_id = new_node_id
    state.tree_first_delivered = True
    scene_dict = materialized["scene"]
    finished = not materialized.get("children")
    state.delivered_scenes += 1
    _append_transcript(req.session_id, {
        "scene_number": state.delivered_scenes,
        "player_choice": f"(свой вариант) {custom_text}",

        **scene_dict,
    })
    _save_snapshot(req.session_id, state)
    return {
        "accepted": True,
        "scene": _scene_with_visuals(state, scene_dict, session_id=req.session_id),
        "state": _public_state(state, reveal_all=finished),
        "finished": finished,

    }


@app.delete("/story/session/{session_id}")
async def drop_session(session_id: str):
    """
    Явный отказ от истории: убираем и из памяти, и с диска. Без этого
    брошенная история всплывала бы как «незаконченная» при каждом входе.
    """
    _safe_session_id(session_id)
    task = TREE_BUILD_TASKS.pop(session_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    async with _session_lock(session_id):
        SESSIONS.pop(session_id, None)
        TREE_BUILD_STATUS.pop(session_id, None)
        try:
            _snapshot_path(session_id).unlink(missing_ok=True)
        except OSError:
            pass
    return {"dropped": True}


async def _peek_session_impl(session_id: str):
    """Восстанавливает текущую сцену без перехода; сериализуется общей блокировкой."""
    state = _get_state(session_id)
    if not state.tree_current_id or not state.story_tree:
        raise HTTPException(status_code=404, detail="История не построена")
    node = state.story_tree.get(state.tree_current_id)
    if node is None:
        raise HTTPException(status_code=404, detail="История не построена")
    if node.get("scene") is None:
        node = await _ensure_materialized(session_id, state, state.tree_current_id)
    else:
        restore_event_path(state, state.story_tree, state.tree_current_id, include_current=True)
    if not state.tree_first_delivered:
        state.tree_first_delivered = True
        state.delivered_scenes += 1
        _append_transcript(session_id, {"scene_number": state.delivered_scenes,
                                      "player_choice": None, **node["scene"]})
    _save_snapshot(session_id, state)
    return {
        "session_id": session_id,
        "scene": _scene_with_visuals(state, node["scene"], session_id=session_id),
        "state": _public_state(state, reveal_all=not node.get("children")),
        "finished": not node.get("children"),

    }


async def _next_scene_impl(req: SceneRequest):
    """Читает готовую сцену или лениво получает выбранный сегмент событий/прозы."""
    state = _get_state(req.session_id)
    _check_scene_position(state, req.from_node_id)

    if not state.tree_current_id or not state.story_tree:
        status = TREE_BUILD_STATUS.get(req.session_id)
        if status and not status.get("done"):
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "tree_not_ready",
                    "message": "История ещё дописывается — подожди готовности.",
                },
            )
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "no_tree",
                "message": "Для этой сессии история не была построена — начни заново.",
            },
        )

    current_node = state.story_tree.get(state.tree_current_id)
    if current_node is None:
        raise HTTPException(status_code=500, detail="Повреждённое состояние дерева истории")

    if not state.tree_first_delivered:
        # Корень уже готов сразу после сборки СКЕЛЕТА дерева, но игрок его
        # ещё не видел — отдаём как есть (материализуя прозу, если нужно),
        # без попытки перейти по req.choice.
        current_node = await _ensure_materialized(req.session_id, state, state.tree_current_id)
        state.tree_first_delivered = True
        scene_dict = current_node["scene"]
        finished = not current_node.get("children")
        # Первая сцена тоже идёт в транскрипт — раньше она терялась совсем,
        # из-за чего в файле оказывалось на одну запись меньше, чем сцен.
        state.delivered_scenes += 1
        _append_transcript(req.session_id, {
            "scene_number": state.delivered_scenes,
            "player_choice": None,

            **scene_dict,
        })
        # Снимок после инкремента — иначе на диске осталась бы версия
        # «до первой сцены» (см. ту же правку в переходах ниже).
        _save_snapshot(req.session_id, state)
        return {
            "scene": _scene_with_visuals(state, scene_dict, session_id=req.session_id),
            "state": _public_state(state, reveal_all=finished),
            "finished": finished,

        }

    children = current_node.get("children", {})
    next_id = children.get(req.choice)
    if next_id is None and not children:
        raise HTTPException(status_code=409, detail="История уже завершена")
    if next_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "invalid_tree_choice",
                "message": "Такого варианта нет на этом узле дерева.",
            },
        )

    # _ensure_materialized saves paid pending events before requesting prose.
    # Ссылка на несуществующий узел: дерево повреждено (битый снимок,
    # оборванное построение). Без этой проверки KeyError уходил бы наружу
    # пятисоткой вместо понятного отказа.
    if next_id not in state.story_tree:
        raise HTTPException(
            status_code=500,
            detail={
                "reason": "broken_tree",
                "message": "Дерево истории повреждено: ветка ведёт в никуда. "
                           "Попробуй загрузить историю заново из .jsonl.",
            },
        )

    next_node = await _ensure_materialized(req.session_id, state, next_id)
    state.tree_current_id = next_id
    scene_dict = next_node["scene"]
    finished = not next_node.get("children")
    state.delivered_scenes += 1
    _append_transcript(req.session_id, {
        "scene_number": state.delivered_scenes,
        "player_choice": req.choice,

        **scene_dict,
    })
    # Снимок здесь, а не только внутри _ensure_materialized: там он делается
    # ДО инкремента delivered_scenes и записи транскрипта, из-за чего
    # восстановление после перезапуска откатывало прогресс (наблюдалось:
    # игрок на 28-й сцене, восстановление вернуло на 15-ю). Теперь состояние
    # на диске совпадает с реальным на момент ответа.
    _save_snapshot(req.session_id, state)
    return {
        "scene": _scene_with_visuals(state, scene_dict, session_id=req.session_id),
        "state": _public_state(state, reveal_all=finished),
        "finished": finished,

    }


def _public_state(state: StoryState, reveal_all: bool = False) -> dict:
    """
    То, что реально нужно фронтенду — без служебных полей.

    Блокнот показывает ТОЛЬКО отношение протагониста к персонажам, не более
    того: для самого протагониста — обычное досье (имя/роль/внешность/
    характер, это про то, кто ты сам, а не отношение к кому-то). Для
    остальных — их имя и то, как они относятся к ГГ (тип связи и
    эмоциональная окраска), не их собственное досье и не связи между
    какими-то двумя второстепенными персонажами между собой.

    reveal_all=True — для финального экрана: раскрывает всех персонажей
    разом, независимо от того, кого игрок реально встретил по ходу
    конкретного прохождения. В остальное время нераскрытому персонажу
    (revealed=False) отдаётся только имя, без чего-либо ещё.
    """
    protagonist_id = next((c.id for c in state.characters.values() if c.is_protagonist), None)

    # Отношение каждого персонажа К ПРОТАГОНИСТУ — не общий список всех
    # связей между всеми парами, только те, что касаются ГГ напрямую.
    relation_to_protagonist: dict[str, tuple[str, str]] = {}
    if protagonist_id:
        for r in state.character_relationships:
            if not (isinstance(r, dict) and isinstance(r.get("between"), list) and len(r["between"]) == 2):
                continue
            a, b = r["between"]
            other = b if a == protagonist_id else (a if b == protagonist_id else None)
            if other is not None:
                relation_to_protagonist[other] = (r.get("type", ""), r.get("attitude", ""))

    characters_public = []
    for c in state.characters.values():
        if c.is_protagonist:
            characters_public.append({
                "id": c.id, "name": c.name, "role": c.role, "traits": c.traits,
                "appearance": c.sprite_appearance or c.appearance, "revealed": True, "is_protagonist": True,
            })
        elif reveal_all or c.revealed:
            rel_type, rel_attitude = relation_to_protagonist.get(c.id, ("", ""))
            characters_public.append({
                "id": c.id, "name": c.name, "revealed": True, "is_protagonist": False,
                "relation_type": rel_type, "relation_attitude": rel_attitude,
                "appearance": c.sprite_appearance or c.appearance,
            })
        else:
            characters_public.append({"id": c.id, "name": c.name, "revealed": False, "is_protagonist": False})

    return {
        "genre": state.genre,
        "node_id": state.tree_current_id,
        "tone": state.tone,
        "location": state.location,
        # Для ИГРОКА прогресс — это сколько он прочитал, а не сколько
        # сгенерировано. current_scene растёт скачком при материализации
        # (весь сегмент применяется разом, +17 за раз), из-за чего корешок
        # прогресса сразу показывал треть истории пройденной на первой же
        # сцене. delivered_scenes растёт по одной на реально показанную.
        "current_scene": state.delivered_scenes or state.current_scene,
        "target_length": state.target_length,
        # Режим экономии — фронт по нему решает, показывать ли свободный
        # ввод: он опирается на события ОБЕИХ веток, а в экономичном режиме
        # вторая ветка не построена, пока игрок в неё не выберет.
        "build_all_branches": state.build_all_branches,
        "progress": round(state.progress_ratio, 2),
        "characters": characters_public,
        "events_count": sum("event" in record for record in state.event_history),
        # Хронология решений игрока — для журнала «Мои решения» на фронте.
        # Служебные "Продолжить" отфильтрованы: это не решения, а перелистывание.
        "player_choices": [c for c in state.player_choices if c and c != "Продолжить"],
        "usage": {
            "input_tokens": state.total_input_tokens,
            "output_tokens": state.total_output_tokens,
            "api_calls": state.total_api_calls,
            "cache_hit_tokens": state.total_cache_hit_tokens,
            "cache_miss_tokens": state.total_cache_miss_tokens,
        },
        "save_warning": getattr(state, '_snapshot_error', None),
    }


# ---------------------------------------------------------- экспорт истории в Markdown


def _render_transcript_markdown(session_id: str) -> str:
    """
    Собирает читаемый .md из игрового транскрипта (.jsonl).

    Транскрипт — машинный формат (по строке JSON на сцену); чтобы показать
    или перечитать историю, нужен человекочитаемый вид. Разметка простая:
    заголовок, сцены по порядку с локацией, репликами и вариантами выбора,
    финал — отдельным блоком. Ничего не додумывает, только перекладывает
    то, что реально записано.
    """
    path = _transcript_path(session_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Транскрипт истории не найден")

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Не удалось прочитать транскрипт: {e}")

    entries = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # битая строка не должна рушить весь экспорт

    if not entries:
        raise HTTPException(status_code=404, detail="В транскрипте нет ни одной сцены")

    out = ["# История", ""]
    # Метаданные прогона — из первого и последнего доступных значений.
    endings = {"victory": "Победа", "death": "Гибель", "failure": "Провал", "complete": "Завершение"}

    for e in entries:
        loc = (e.get("location") or "").strip()
        speaker = e.get("speaker")
        dialogue = e.get("dialogue")
        narration = (e.get("narration") or "").strip()

        header = f"## Сцена {e.get('scene_number', '?')}"
        if loc:
            header += f" — {loc}"
        out.append(header)
        out.append("")

        # Решение игрока, приведшее к этой сцене (кроме служебного «Продолжить»).
        pc = e.get("player_choice")
        if pc and pc != "Продолжить":
            out.append(f"> Ваш выбор: {pc}")
            out.append("")

        if e.get('blocks'):
            for block in e['blocks']:
                text = block.get('text', '')
                if block.get('type') == 'dialogue':
                    text = f"**{block.get('speaker') or block.get('speaker_id')}:** {text}"
                elif block.get('type') == 'thought':
                    text = f"*{text}*"
                out.extend([text, ''])
        elif narration:
            out.append(narration)
            out.append("")

        if not e.get('blocks') and speaker and dialogue:
            # Реплику НЕ оборачиваем в Markdown-цитату: в самом тексте диалога
            # часто уже есть тире («— сказал он»), и «> — …» давало двойной
            # маркер. Пишем жирным говорящего и текст как есть.
            out.append(f"**{speaker}:** {str(dialogue).strip()}")
            out.append("")

        choices = e.get("choices") or []
        if choices:
            out.append("**Варианты:**")
            for c in choices:
                out.append(f"- {c}")
            out.append("")

        ending = e.get("ending")
        if ending:
            out.append(f"### Финал: {endings.get(ending, ending)}")
            out.append("")

    return "\n".join(out).rstrip() + "\n"



@app.get("/story/export/{session_id}.md")
async def export_markdown(session_id: str):
    """
    Отдаёт историю как .md-файл для скачивания. Имя с расширением прямо в
    пути, чтобы браузер и curl сохраняли файл без лишних заголовков.
    """
    _safe_session_id(session_id)
    md = _render_transcript_markdown(session_id)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{session_id}.md"',
        },
    )


# ---------------------------------------------------------- раздача фронтенда
#
# Всё, что ниже, регистрируется ПОСЛЕДНИМ — FastAPI матчит роуты по порядку
# объявления, поэтому /health и /story/* выше уже забирают себе эти пути,
# а сюда попадает только то, что не подошло ни одному API-роуту.


def _resolve_frontend_dist() -> Path:
    """
    Три сценария, в порядке приоритета:
      1. STORY_ENGINE_FRONTEND_DIST — явное указание (полезно для нестандартной
         раскладки после упаковки).
      2. sys._MEIPASS — PyInstaller распаковывает собранный .exe во временную
         директорию и кладёт путь сюда; при обычном запуске этого атрибута
         у sys нет вообще, так что getattr с дефолтом None безопасен.
      3. Путь относительно исходников (frontend/dist рядом с backend/) —
         обычный режим разработки и запуска из исходников.
    """
    import os
    import sys

    if env_path := os.environ.get("STORY_ENGINE_FRONTEND_DIST"):
        return Path(env_path)

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "frontend_dist"

    return Path(__file__).parent.parent / "frontend" / "dist"


FRONTEND_DIST = _resolve_frontend_dist()


# ── Обёртки с блокировкой по сессии ───────────────────────────────────────
# Тела вынесены в _*_impl, а сюда — только захват блокировки. Все три
# эндпоинта меняют состояние и могут запустить генерацию: параллельный
# вход в них означал бы двойную оплату одного и того же сегмента.


@app.post("/story/scene")
async def next_scene(req: SceneRequest):
    async with _session_lock(req.session_id):
        return await _next_scene_impl(req)


@app.post("/story/custom_choice")
async def custom_choice(req: CustomChoiceRequest):
    async with _session_lock(req.session_id):
        return await _custom_choice_impl(req)


@app.post("/story/resume_at")
async def resume_at(req: ResumeRequest):
    async with _session_lock(req.session_id):
        return await _resume_at_impl(req)


@app.get("/story/session/{session_id}")
async def peek_session(session_id: str):
    async with _session_lock(session_id):
        return await _peek_session_impl(session_id)


# Только компактный набор дневных фонов; кэш модели и исходная папка Colab не публикуются.
AMBIENCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "ambience"
MUSIC_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "music_catalogs"
@app.get("/music/{filename}", include_in_schema=False)
async def music_file(filename: str):
    catalog = get_music_catalog()
    track = next((item for item in catalog.tracks if item.url == f"/music/{filename}"), None)
    if not track:
        raise HTTPException(status_code=404, detail="Unknown music track")
    if track.download_url:
        try:
            path = await get_music_assets().ensure(track)
        except MusicUnavailable:
            raise HTTPException(status_code=503, detail="Music is unavailable; cached tracks remain playable")
        return FileResponse(path, media_type="audio/mpeg",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})
    if track.file:
        path = (catalog.path.parent / track.file).resolve()
        if path.is_relative_to(catalog.path.parent.resolve()) and path.is_file():
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="Music file is unavailable")


@app.get("/music-credits", include_in_schema=False)
async def music_credits():
    return FileResponse(MUSIC_DIR / "CREDITS.html", media_type="text/html")


@app.get("/characters/{filename}", include_in_schema=False)
async def character_file(filename: str):
    path = get_sprite_catalog().file(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Character image is unavailable")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


app.mount("/ambience", StaticFiles(directory=AMBIENCE_DIR, check_dir=False), name="ambience")
app.mount("/backgrounds", StaticFiles(directory=background_directory(), check_dir=False), name="backgrounds")

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/")
    async def frontend_index():
        return FileResponse(FRONTEND_DIST / "index.html")
else:
    @app.get("/")
    async def frontend_not_built():
        return {
            "detail": (
                "Фронтенд не собран. Из frontend/: npm install && npm run build, "
                "затем перезапусти сервер."
            )
        }
