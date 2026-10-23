"""
Обёртка над DeepSeek API.

Разные вызовы с разной температурой:
  - generate_segment_prose / generate_segment_events: температура повыше
    (живость текста, разнообразие событий)
  - generate_custom_choice_events: продолжение по действию игрока

Произвольное действие проверяется при планировании; новый сегмент событий
проверяется одним запросом перед прозой. Замены применяются локально и атомарно.
Редактора прозы, модерации и сжатия нет; thinking отключён во всех запросах.

Учёт расхода токенов идёт из поля usage реальных ответов API, а не по оценке
словами — каждый метод, дёргающий _call, принимает state (или None, если
вызов делается вне контекста конкретной сессии) и пишет расход прямо туда,
потому что сам клиент пересоздаётся на каждый HTTP-запрос сервера и не может
накапливать счётчик в себе между вызовами.
"""

import json
import time
import uuid

from deepseek_logging import write_record, current_attempt
from segment_patches import apply_replacements
from prompts import build_segment_review_prompt

import httpx
from character_sprites import assign_sprites, apply_character_scene
from character_appearance import normalize_appearance, describe_appearance
from story_events import normalize_event, bind_event, through_ending, terminal_event
from prose_blocks import normalize_prose
from visual_retrieval import get_visual_catalog

from character_identity import prepare_cast, dialogue_fields
from state_machine import StoryState, AgeValidationError
from background_effects import scene_effect_fields
from scene_conditions import advance_conditions
from prompts import (
    SYSTEM_PROMPT,
    PROSE_RULES,
    MODERN_PROSE_METADATA,
    build_scene_system_prompt,
    build_genre_directive,
    build_tone_directive,
    build_clarifying_prompt,
    build_premise_suggestions_prompt,
    build_genre_tone_fit_prompt,
    build_story_plan_prompt,
    build_character_planning_prompt,
    build_segment_plan_directive,
    build_story_canon_prompt,
    choice_window_count,
    build_segment_events_prompt,
    build_segment_prose_prompt,
    build_custom_choice_events_prompt,
    build_custom_choice_validation_prompt,
)

DEEPSEEK_MODEL = "deepseek-flash"


def _parse_json_object(raw: str) -> dict:
    """
    Разбирает ответ модели, ГАРАНТИРУЯ словарь на выходе.

    Модель обычно возвращает объект, но может вернуть валидный JSON другого
    типа — "null", список, число. Вызывающий код везде ждёт словарь и падал
    бы на .get() с AttributeError, которого НИКТО не ловит: все except-ы
    перечисляют JSONDecodeError и KeyError. Приводим такой случай к тому же
    JSONDecodeError, чтобы он шёл по уже существующим путям обработки
    (ретрай, отказ), а не выходил наружу пятисоткой.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise json.JSONDecodeError(
            f"Ожидался JSON-объект, получен {type(data).__name__}", raw or "", 0
        )
    return data


def _as_str_list(value) -> list[str]:
    """
    Приводит поле ответа модели к списку строк.

    Модель иногда возвращает строку вместо списка ("Найден пистолет" вместо
    ["Найден пистолет"]). Прямая итерация по такой строке разбирает её
    посимвольно и тихо забивает состояние мусором — это хуже падения,
    потому что незаметно. Здесь строка становится списком из одного элемента,
    всё остальное непригодное отбрасывается.
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v).strip()]


class DeepSeekClient:
    def __init__(self, api_key: str | None = None,
                 *, session_id: str | None = None, log_dir=None):
        # Ключ берётся только из config.json (см. config_store) — один
        # источник правды, без фолбэка на переменную окружения. Передача
        # api_key=None сюда означает, что ключ ещё не сохранён.
        self.api_key = api_key
        if not self.api_key:
            raise RuntimeError("Ключ DeepSeek не задан")
        # Отдельный клиент на вызов закрывается вместе с запросом; таймауты
        # задаются в httpx и не оставляют фоновые потоки без лимита ожидания.
        self._base_url = "https://api.deepseek.com"
        self._headers = {"Authorization": f"Bearer {self.api_key}"}
        self._timeout = httpx.Timeout(connect=30.0, read=180.0, write=60.0, pool=30.0)
        self._last_usage: dict = {}
        self.session_id = session_id
        self.log_dir = log_dir

    def _call(
        self,
        messages: list[dict],
        temperature: float,
        json_mode: bool = True,
        state: StoryState | None = None,
        operation: str = "unspecified",
    ) -> str:
        """
        state передаётся сюда, а не наружу отдельным возвращаемым значением —
        каждый вызывающий метод и так получает state параметром, и запись
        расхода сразу в момент вызова проще, чем пробрасывать usage через
        всю цепочку return'ов до места, где state точно есть.
        """
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": messages,
            "temperature": temperature,
            # Единая настройка для всех операций, включая меню и проверки.
            "thinking": {"type": "disabled"},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        request_id = uuid.uuid4().hex
        context = {
            "request_id": request_id,
            "generation_id": request_id,
            "attempt": 1,
            **current_attempt(),
            "operation": operation,
            "session_id": self.session_id,
        }
        if state is not None:
            context["story"] = {
                name: getattr(state, name, None)
                for name in ("current_scene", "segment_index", "branch_path", "tree_current_id")
            }

        def log(event, **fields):
            write_record({**context, "event": event, **fields},
                         api_key=self.api_key, log_dir=self.log_dir)

        self._last_usage = {}
        log("request", method="POST", url=self._base_url + "/chat/completions", payload=payload)
        started = time.perf_counter()
        resp = None
        try:
            # Отдельный клиент на вызов сохраняет изоляцию соединений потоков.
            # Повторные попытки по-прежнему выполняет вызывающий код.
            with httpx.Client(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self._timeout,
            ) as client:
                resp = client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()

            if not isinstance(data, dict):
                raise KeyError("API response must be an object")
            raw_usage = data.get("usage") or {}
            if not isinstance(raw_usage, dict):
                raw_usage = {}

            def count(value):
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return 0

            usage = {
                "input_tokens": count(raw_usage.get("prompt_tokens")),
                "output_tokens": count(raw_usage.get("completion_tokens")),
                "cache_hit_tokens": count(raw_usage.get("prompt_cache_hit_tokens")),
                "cache_miss_tokens": count(raw_usage.get("prompt_cache_miss_tokens")),
            }
            self._last_usage = usage
            if state is not None:
                state.record_usage(raw_usage)
            choices = data.get('choices')
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise KeyError('API response has no completion')
            message = choices[0].get('message')
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise KeyError('API completion has no text')
        except Exception as error:
            log("error", elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                status_code=resp.status_code if resp is not None else None,
                response_body=resp.text if resp is not None else None,
                usage=dict(self._last_usage),
                error={"type": type(error).__name__, "message": str(error)})
            raise
        log("response", elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            status_code=resp.status_code, response_body=resp.text, usage=usage)
        return content

    # ---------- Онбординг ----------

    def generate_clarifying_questions(
        self, seed: str, genre: str, tone: str, state: StoryState | None = None,
    ) -> list[dict]:
        """
        Шаг онбординга: 3 уточняющих вопроса с вариантами ответа. Жанр/тон
        к этому моменту уже выбраны игроком (порядок шагов: затравка -> жанр
        и тон -> уточняющие вопросы) — вопросы строятся конкретно под уже
        известный жанр, а не абстрактно про "желаемый тон", который к этому
        моменту уже не нужно выяснять.

        Варианты — кнопки, а не свободный ввод: это и быстрее для игрока,
        и меньше поверхности для протаскивания проблемных формулировок.
        state обычно None здесь — сессия ещё не создана на этом шаге,
        считать эти токены не в кого; они просто не попадут в счётчик истории.
        """
        raw = self._call(
            operation="generate_clarifying_questions",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_clarifying_prompt(seed, genre, tone)},
            ],
            temperature=0.6,
            state=state,
        )
        # Здесь список — ЗАКОННЫЙ ответ, не ошибка: модель может вернуть как
        # готовый массив вопросов, так и {"questions": [...]}. Поэтому обычный
        # json.loads, а не _parse_json_object, который требует объект.
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("questions", [])
        questions = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get('question'), str):
                continue
            options = item.get('options')
            if not isinstance(options, list):
                continue
            options = list(dict.fromkeys(o.strip() for o in options if isinstance(o, str) and o.strip()))
            if item['question'].strip() and options:
                questions.append({'question': item['question'].strip(), 'options': options})
        return questions

    def generate_premise_suggestions(self) -> list[dict]:
        """
        Три коротких затравки для экрана онбординга — не план (без переломных
        точек, без затрат на структуру), просто творческая подсказка. Не
        используем полный SYSTEM_PROMPT — та рамка заточена под уже идущую
        историю с персонажами и возрастными ограничениями, здесь нечего
        валидировать в этом смысле, это просто три коротких предложения.

        Вызывается заново при каждом открытии экрана — сознательный выбор
        (не статичный захардкоженный список), несмотря на лишний сетевой
        вызов до того, как игрок вообще решил играть.
        """
        raw = self._call(
            operation="generate_premise_suggestions",
            messages=[{"role": "user", "content": build_premise_suggestions_prompt()}],
            temperature=0.9,  # выше обычного — здесь важнее разнообразие, чем точность
        )
        data = _parse_json_object(raw)
        premises = data.get("premises")
        if not isinstance(premises, list):
            raise KeyError('API response has no premise suggestions')
        suggestions = [
            {"genre": str(p.get("genre", "")), "text": p["text"].strip()}
            for p in premises
            if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip()
        ]
        if not suggestions:
            raise KeyError('API response has no premise suggestions')
        return suggestions

    def generate_genre_tone_fit(
        self, seed: str, genres: list[str], tones: list[str],
    ) -> tuple[list[str], list[str]]:
        """
        Фильтрует переданные списки жанров/тонов под конкретную затравку —
        не придумывает новые, только сужает то, что уже есть. Как и остальные
        операции, использует reasoning low; сбой оставляет все варианты видимыми.

        Код не доверяется промпту вслепую (тот факт, что промпт просит
        "не меньше 3" и "бери слово в слово", не гарантирует, что модель
        это соблюдёт): результат явно пересекается с реально переданными
        списками (нельзя вернуть жанр, которого не было среди вариантов),
        и принудительно откатывается к полному списку, если отфильтрованный
        получился короче 3 — недостаточно узкий фильтр хуже, чем никакого.
        """
        prompt = build_genre_tone_fit_prompt(seed, genres, tones)
        try:
            raw = self._call(
                operation="generate_genre_tone_fit",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,  # здесь важна точность соответствия, не разнообразие
            )
            data = _parse_json_object(raw)
            raw_genres, raw_tones = data.get("genres"), data.get("tones")
            fitted_genres = [g for g in raw_genres if g in genres] if isinstance(raw_genres, list) else []
            fitted_tones = [t for t in raw_tones if t in tones] if isinstance(raw_tones, list) else []
            if len(fitted_genres) < 3:
                fitted_genres = genres
            if len(fitted_tones) < 3:
                fitted_tones = tones
            return fitted_genres, fitted_tones
        except (httpx.HTTPError, json.JSONDecodeError, KeyError):
            return genres, tones

    # ---------- Генерация сцены ----------

    def generate_story_plan(
        self, seed: str, genre: str, tone: str,
        target_length: int, clarifications: list[str], state: StoryState | None = None,
    ) -> tuple[dict, dict]:
        """
        Разовый вызов при старте истории — не на каждую сцену. Даёт пошаговой
        генерации направление, вместо того чтобы импровизировать вслепую
        с нуля на каждом шаге.

        Возвращает (план, usage) — usage это ДЕЛЬТА токенов именно этого
        вызова (снимок до и после), не общий счётчик state с начала истории.
        Нужно для диагностического лога — знать реальную цену каждого шага
        по отдельности, не только суммарно.
        """
        before_in = state.total_input_tokens if state else 0
        before_out = state.total_output_tokens if state else 0
        before_hit = state.total_cache_hit_tokens if state else 0
        before_miss = state.total_cache_miss_tokens if state else 0

        prompt = build_story_plan_prompt(seed, genre, tone, target_length, clarifications)
        raw = self._call(
            operation="generate_story_plan",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            state=state,
        )
        plan = _parse_json_object(raw)

        usage = {
            "input_tokens": (state.total_input_tokens - before_in) if state else 0,
            "output_tokens": (state.total_output_tokens - before_out) if state else 0,
            "cache_hit_tokens": (state.total_cache_hit_tokens - before_hit) if state else 0,
            "cache_miss_tokens": (state.total_cache_miss_tokens - before_miss) if state else 0,
        }

        # Валидируем форму плана здесь же — если модель дала что-то кривое,
        # лучше вернуть безопасный пустой план (build_segment_plan_directive корректно
        # обрабатывает None/пустое), чем протащить мусор в state.story_plan.
        if not isinstance(plan, dict):
            return {"segments": []}, usage
        if "segments" in plan:
            if not isinstance(plan["segments"], list):
                raise KeyError("segments должен быть списком")
            # Only source-format validation, no paid review or plot correction.
            expected = choice_window_count(target_length) + 1
            if len(plan["segments"]) != expected:
                raise KeyError("неверное число заготовок сегментов")
            for segment in plan["segments"]:
                if not isinstance(segment, dict) or any(
                    not isinstance(segment.get(key), list)
                    or any(not isinstance(value, str) for value in segment[key])
                    for key in ("places", "events")
                ):
                    raise KeyError("сегмент должен содержать списки places/events")
        elif not isinstance(plan.get("turning_points"), list):
            plan["turning_points"] = []  # legacy plans remain readable
        return plan, usage

    def generate_characters(
        self, seed: str, genre: str, tone: str, plan: dict,
        count_min: int, count_max: int, state: StoryState | None = None,
    ) -> tuple[list[dict], list[dict], dict]:
        """
        Создаёт каст и начальные связи одним запросом. Поля приводятся к
        используемым типам без оценки характера, ролей или отношений.
        ID внешности берутся из общего локального пула; описание собирается кодом.
        Возраст нормализуется при добавлении персонажа в StoryState.
        """
        before_in = state.total_input_tokens if state else 0
        before_out = state.total_output_tokens if state else 0
        before_hit = state.total_cache_hit_tokens if state else 0
        before_miss = state.total_cache_miss_tokens if state else 0

        initial_context = "\n".join(record["context"] for record in state.event_history
            if isinstance(record.get("context"), str)) if state else ""
        prompt = build_character_planning_prompt(seed, genre, tone, plan, count_min, count_max, initial_context)
        raw = self._call(
            operation="generate_characters",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            state=state,
        )
        data = _parse_json_object(raw)

        usage = {
            "input_tokens": (state.total_input_tokens - before_in) if state else 0,
            "output_tokens": (state.total_output_tokens - before_out) if state else 0,
            "cache_hit_tokens": (state.total_cache_hit_tokens - before_hit) if state else 0,
            "cache_miss_tokens": (state.total_cache_miss_tokens - before_miss) if state else 0,
        }

        chars = data.get("characters")
        if not isinstance(chars, list):
            chars = []
        chars = [c for c in chars if isinstance(c, dict) and c.get("name")]
        chars, identity_rels = prepare_cast(chars, data.get("relationships", []))
        for c in chars:
            c["traits"] = ", ".join(_as_str_list(c.get("traits")))
            c['kind'] = c.get('kind') if c.get('kind') in ('human', 'nonhuman') else 'human'
            if c['kind'] == 'nonhuman':
                c['appearance_features'] = {'gender': 'unknown'}
                c['appearance'] = 'Нечеловеческий персонаж; абстрактная сфера.'
                continue
            features = normalize_appearance(c.get("appearance"), seed=f"{seed}:{c['id']}:{c['name']}")
            appearance = c.get('appearance')
            gender = appearance.get('gender') if isinstance(appearance, dict) else None
            if gender not in ('man', 'woman'):
                features['gender'] = 'unknown'
            c["appearance_features"] = features
            c["appearance"] = describe_appearance(features)

        rels = identity_rels
        if not isinstance(rels, list):
            rels = []
        valid_rels = []
        for r in rels:
            if not isinstance(r, dict):
                continue
            between = r.get("between")
            if not isinstance(between, list) or len(between) != 2:
                continue
            rel_type = str(r.get("type", ""))
            attitude = str(r.get("attitude", ""))
            valid_rels.append({"between": between, "type": rel_type, "attitude": attitude})

        return chars, valid_rels, usage


    @staticmethod
    def _story_messages(state: StoryState, task: str, *, planning: bool = False) -> list[dict]:
        # DeepSeek automatically caches matching prefixes. Keep immutable canon
        # and append-only source history ahead of task-specific/dynamic content.
        # Catalog and creative planning hints follow history. Prose has a separate
        # segment-only context assembled by _prose_messages.
        if planning:
            task = (get_visual_catalog().prompt_catalog() + build_genre_directive(state.genre)
                    + build_tone_directive(state.tone) + "\n" + task)
        return [
            {"role": "system", "content": build_scene_system_prompt(
                state.genre, state.tone, state.characters_block()) + build_story_canon_prompt(state.story_plan)},
            {"role": "user", "content": state.memory_prompt_block() + "\n" + task},
        ]

    def validate_custom_choice(
        self, state: StoryState, option_a: str, option_b: str,
        events_a: list, events_b: list, custom_text: str,
    ) -> tuple[dict, dict]:
        """Проверяет действие и либо планирует путь, либо предлагает три исправления."""
        raw = self._call(
            operation="validate_custom_choice",
            messages=self._story_messages(state, build_custom_choice_validation_prompt(
                    state.to_prompt_block(include_memory=False), option_a, option_b, events_a, events_b,
                    custom_text, state.target_length - state.current_scene), planning=True),
            temperature=0.4,
            state=state,
        )
        data = _parse_json_object(raw)
        if not isinstance(data.get("accepted"), bool):
            raise KeyError("проверка действия не вернула accepted")
        result = {"accepted": data["accepted"], "reason": "",
                  "clarifying_question": "", "clarifying_options": [], "events": []}
        if result["accepted"]:
            events = data.get("resulting_events")
            if not isinstance(events, list) or not events:
                raise KeyError("ответ принятого действия не содержит событий")
            result["events"] = through_ending([normalize_event(e, state.characters, default_version=3) for e in events])
        else:
            options = data.get("clarifying_options")
            if (not isinstance(options, list) or len(options) != 3
                    or any(not isinstance(o, str) or not o.strip() or len(o.strip()) > 500 for o in options)):
                raise KeyError("ожидались три допустимых варианта действия")
            options = [o.strip() for o in options]
            if len({o.casefold() for o in options}) != 3:
                raise KeyError("варианты исправления действия должны различаться")
            question = data.get("clarifying_question")
            if not isinstance(question, str) or not question.strip():
                raise KeyError("не задан вопрос для уточнения действия")
            result.update(reason=str(data.get("reason") or ""),
                          clarifying_question=question.strip(), clarifying_options=options)
        return result, dict(self._last_usage)

    def generate_custom_choice_events(
        self, state: StoryState, custom_text: str,
    ) -> tuple[list, dict]:
        """Планирует последствия выбранного игроком ранее предложенного исправления."""
        raw = self._call(
            operation="generate_custom_choice_events",
            messages=self._story_messages(state, build_custom_choice_events_prompt(
                    state.to_prompt_block(include_memory=False), custom_text,
                    state.target_length - state.current_scene), planning=True),
            temperature=0.85,
            state=state,
        )
        data = _parse_json_object(raw)
        raw_events = data.get("events")
        if not isinstance(raw_events, list) or not raw_events:
            raise KeyError("ответ продолжения не содержит непустой список событий")
        return through_ending([normalize_event(e, state.characters, default_version=3) for e in raw_events]), dict(self._last_usage)

    def generate_segment_events(
        self, state: StoryState, start_scene: int, ceiling_scene: int,
        window: tuple[int, int] | None,
    ) -> tuple[list[str], list[str] | None, dict]:
        """
        Один запрос — события одного сегмента до выбора или финала.
        Экономный режим вызывает его после решения игрока; обычный также
        готовит два ближайших продолжения, без глубоких веток. Получает
        весь журнал сырого пути и только текущую заготовку сегмента.

        Возвращает (тексты событий, сырые варианты пивотного выбора или
        None, usage). Сырые — код здесь только извлекает то, что дала
        модель (последнее событие, поле "choices", если оно есть), но НЕ
        проверяет позицию относительно окна и число вариантов — эта
        защита осознанно оставлена в server.py, там же, где живёт
        остальная защита того же типа (не размазываем одну логику по
        двум разным файлам).
        """
        before_in = state.total_input_tokens
        before_out = state.total_output_tokens
        before_hit = state.total_cache_hit_tokens
        before_miss = state.total_cache_miss_tokens

        plan_directive = build_segment_plan_directive(
            state.story_plan, start_scene, ceiling_scene, state.target_length, state.segment_index
        )
        prompt = build_segment_events_prompt(
            state.to_events_prompt_block(include_memory=False), plan_directive, start_scene, ceiling_scene,
            window,
        )
        raw = self._call(
            operation="generate_segment_events",
            messages=self._story_messages(state, prompt, planning=True),
            temperature=0.85,
            state=state,
        )
        data = _parse_json_object(raw)

        usage = {
            "input_tokens": state.total_input_tokens - before_in,
            "output_tokens": state.total_output_tokens - before_out,
            "cache_hit_tokens": state.total_cache_hit_tokens - before_hit,
            "cache_miss_tokens": state.total_cache_miss_tokens - before_miss,
        }

        raw_events = data["events"]  # KeyError, если отсутствует — тот же except, что и битый JSON
        if not isinstance(raw_events, list) or not raw_events:
            raise KeyError("ответ планирования событий не содержит непустой список")

        valid_events = through_ending([normalize_event(e, state.characters, default_version=3) for e in raw_events])
        if not valid_events:
            raise KeyError("ни одно событие в ответе не прошло базовую проверку")

        # Пивотный выбор ищем ТОЛЬКО у последнего элемента исходного (не
        # отфильтрованного) списка — так и было задумано промптом. Если
        # модель по ошибке дала "choices" где-то в середине, это здесь
        # осознанно игнорируется: единственная позиция, которую вообще
        # рассматриваем — последняя.
        pivot_choices = None
        last_raw = raw_events[-1] if isinstance(raw_events[-1], dict) else {}
        candidate = last_raw.get("choices")
        if candidate is not None and candidate != []:
            if (not isinstance(candidate, list) or len(candidate) != 2
                    or any(not isinstance(c, str) or not c.strip() for c in candidate)
                    or candidate[0].strip().casefold() == candidate[1].strip().casefold()):
                raise KeyError("обычная развилка должна содержать два разных текста выбора")
            pivot_choices = [c.strip() for c in candidate]

        return valid_events, pivot_choices, usage


    def review_segment_events(self, state, events, *, final=False):
        """One review of the whole draft. All replacements are applied by Python."""
        before = (state.total_input_tokens, state.total_output_tokens,
                  state.total_cache_hit_tokens, state.total_cache_miss_tokens)
        prompt = build_segment_review_prompt(state.to_prompt_block(include_memory=False),
                                             events, final=final)
        raw = self._call(operation="review_segment_events", messages=self._story_messages(state, prompt),
                         temperature=0.2, state=state)
        review = _parse_json_object(raw)
        corrected = apply_replacements(events, review, state.characters, final=final)
        after = (state.total_input_tokens, state.total_output_tokens,
                 state.total_cache_hit_tokens, state.total_cache_miss_tokens)
        usage = dict(zip(("input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens"),
                         (end - start for start, end in zip(before, after))))
        return corrected, review['replacements'], usage

    def generate_segment_prose(
        self, state: StoryState, events: list[str], choice_texts: tuple[str, str] | None,
    ) -> tuple[list[dict], dict]:
        """
        Дорогой этап — вызывается ЛЕНИВО, только когда игрок реально дошёл
        до этого сегмента (см. /story/scene в server.py). events уже решены
        заранее (generate_segment_events) — эта генерация только одевает их
        в текст, не придумывает заново, что происходит.

        Получает только события текущего сегмента и стабильный справочник каста.
        Журнал прошлого, общий план и скрытые цели в прозу не передаются.

        Возвращает сцены для непосредственного применения к состоянию.
        Отдельных проверок связности и исправлений нет.
        """
        before_in = state.total_input_tokens
        before_out = state.total_output_tokens
        before_hit = state.total_cache_hit_tokens
        before_miss = state.total_cache_miss_tokens

        prompt = build_segment_prose_prompt(events, choice_texts, include_rules=False)
        raw = self._call(
            operation="generate_segment_prose",
            messages=self._prose_messages(state, prompt),
            temperature=0.85,
            state=state,
        )
        data = _parse_json_object(raw)

        usage = {
            "input_tokens": state.total_input_tokens - before_in,
            "output_tokens": state.total_output_tokens - before_out,
            "cache_hit_tokens": state.total_cache_hit_tokens - before_hit,
            "cache_miss_tokens": state.total_cache_miss_tokens - before_miss,
        }

        scenes = data["scenes"]  # KeyError, если отсутствует — попадает в тот же except, что и битый JSON
        if not isinstance(scenes, list) or not scenes:
            raise KeyError("ответ сегмента не содержит непустой список сцен")
        modern = any(isinstance(e, dict) and e.get("v") in (2, 3) for e in events)
        # Surplus text strictly after a planned ending is safely discarded locally.
        # Missing/interior scenes cannot be repaired without shifting event indices.
        if modern and events and terminal_event(events[-1]):
            scenes = scenes[:len(events)]
        if modern and (len(scenes) != len(events)
                       or any(not isinstance(scene, dict) for scene in scenes)):
            raise KeyError("число/структура сцен не соответствует событиям сегмента")
        valid_scenes = [normalize_prose(scene, state.characters)
                        for scene in scenes if isinstance(scene, dict)]
        nonempty = lambda scene: scene.get("blocks") or (
            isinstance(scene.get("narration"), str) and scene["narration"].strip())
        if modern and any(not nonempty(scene) for scene in valid_scenes):
            raise KeyError("пустая сцена нарушает соответствие событиям сегмента")
        # Preserve unfinished legacy text plans; modern plans always bind one-to-one.
        valid_scenes = [scene for scene in valid_scenes if nonempty(scene)]
        if not valid_scenes:
            raise KeyError("ни одна сцена не прошла базовую проверку структуры")
        for scene in valid_scenes:
            for field in ('dialogue', 'speaker', 'speaker_id'):
                if field in scene and not isinstance(scene[field], str):
                    scene.pop(field)

        return valid_scenes, usage

    @staticmethod
    def _prose_messages(state: StoryState, prompt: str) -> list[dict]:
        # A stable prose-specific prefix contains identity/style, never plot secrets.
        # Only the current segment is sent as narrative input, without the journal,
        # pending choice, story outline, canon or derived state from earlier segments.
        cast = [{"id": c.id, "name": c.name, "age": c.age, "kind": c.kind,
                 "gender": c.appearance_features.get("gender", "unknown"),
                 "role": c.role, "appearance": c.appearance, "traits": c.traits,
                 "is_protagonist": c.is_protagonist}
                for c in state.characters.values()]
        cast_block = "\n[СПРАВОЧНИК ПЕРСОНАЖЕЙ: ID, ИМЕНА, ВНЕШНОСТЬ И МАНЕРА РЕЧИ]\n" + json.dumps(
            cast, ensure_ascii=False, separators=(",", ":"))
        return [
            {"role": "system", "content": build_scene_system_prompt(
                state.genre, state.tone, cast_block) + build_tone_directive(state.tone)
                + "\n" + PROSE_RULES + MODERN_PROSE_METADATA},
            {"role": "user", "content": prompt},
        ]


    @staticmethod
    def apply_scene_to_state(
        state: StoryState, parsed: dict, user_choice: str | None = None,
        *, legacy_locations: bool = False,
    ) -> dict:
        """
        Применяет сырой ответ модели к стейт-машине.

        Статический метод — self нигде не используется в основном теле, а
        возвращаемый dict содержит принятые метаданные локации и условий сцены.
        legacy_locations нужен только при воспроизведении старых JSONL,
        в которых ещё не было контракта явного перехода.

        Это важно:
        реконструкция сессии из .jsonl (_reconstruct_session в
        server.py) должна уметь переиграть уже известные, ранее
        сгенерированные сцены без создания DeepSeekClient вообще — а
        конструктор клиента требует API-ключ, который для чистого
        воспроизведения старых данных не нужен.

        Применение сцены и воспроизведение журнала не вызывают модель.

        Ко всему, что пришло от модели, относимся как к недоверенным данным:
        поля могут отсутствовать, иметь неверный тип или быть мусором.
        Числовой возраст персонажа ниже 18 нормализуется в Character.
        Отсутствующий или нечисловой возраст остаётся ошибкой формата.
        """
        if isinstance(parsed.get("_planned_event"), dict) and parsed["_planned_event"].get("v") in (2, 3):
            parsed = bind_event(parsed, parsed["_planned_event"])
        if user_choice:
            state.add_player_choice(user_choice)

        new_char = parsed.get("new_character")
        # .strip() обязателен: имя из одних пробелов проходит проверку на
        # непустоту, но отклоняется в add_character — и роняло бы разбор
        # всей сцены исключением вместо тихого пропуска одного персонажа.
        if (isinstance(new_char, dict) and str(new_char.get("name") or "").strip()
                and not state.character_id(new_char.get("id") or new_char["name"])
                and not any(c.name == str(new_char["name"]).strip() for c in state.characters.values())):
            raw_age = new_char.get("age")
            try:
                age = int(raw_age)
            except (TypeError, ValueError):
                # Нормализуем только явно указанный числовой возраст.
                raise AgeValidationError(
                    f"Персонаж '{new_char.get('name')}' отклонён: "
                    f"возраст не указан или не является числом ({raw_age!r})."
                )
            state.add_character(
                name=str(new_char["name"]),
                age=age,
                role=str(new_char.get("role", "")),
                traits=str(new_char.get("traits", "")),
                appearance=str(new_char.get("appearance", "")),
                character_id=new_char.get("id"),
            )

        planned_event = parsed.get("_planned_event")
        from scene_locations import apply_scene_location
        location_fields = apply_scene_location(state, parsed, legacy=legacy_locations)

        # Время и погода сохраняются до изменения; локальные источники
        # и одноразовые события нужно заново запрашивать в текущей сцене.
        state.scene_conditions = advance_conditions(state.scene_conditions, parsed)
        scene_fields = {**location_fields, **scene_effect_fields({**state.scene_conditions,
                        "screen_events": parsed.get("screen_events")})}

        # Text stays available to local UI/asset matching. Story prompts use
        # only raw source events; recent_lines is never included in them.
        narration = parsed.get("narration")
        if isinstance(narration, str) and narration.strip():
            state.add_line("(нарратор)", narration)

        for block in parsed.get('blocks', []):
            if block.get('type') == 'dialogue':
                state.add_line(block['speaker_id'], block['text'])
                state.touch_character(block['speaker_id'])
        identity = dialogue_fields(state.characters, parsed)
        dialogue = parsed.get("dialogue")
        if isinstance(dialogue, str) and dialogue.strip():
            if identity["speaker_id"]:
                state.add_line(identity["speaker_id"], dialogue)
                state.touch_character(identity["speaker_id"])
            elif identity["speaker"]:
                state.add_line(identity["speaker"], dialogue, resolve=False)
        else:
            identity = {"speaker_id": None, "speaker": None}
        scene_fields.update(identity)
        assign_sprites(state)
        scene_fields.update(apply_character_scene(state, {**parsed, **identity}))

        if isinstance(planned_event, dict):
            for name in planned_event.get("participants", []):
                state.touch_character(name)
        legacy_changes = None
        if not isinstance(planned_event, dict) or planned_event.get("v") not in (2, 3):
            legacy_changes = {key: parsed[key] for key in
                ("new_facts", "entities", "resources", "world_rules", "new_open_threads",
                 "closed_threads", "location", "location_id", "location_transition", "ending",
                 "time_of_day", "atmosphere", "conditions_change", "ambient_effect",
                 "screen_events") if parsed.get(key)}
        if planned_event is not None:
            state.record_event(planned_event, legacy_changes=legacy_changes)
        elif legacy_changes:
            state.record_event({"legacy_changes": legacy_changes})
        state.advance_scene()

        return scene_fields
