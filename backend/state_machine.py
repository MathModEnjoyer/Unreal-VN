"""Состояние игры и полный журнал исходных событий текущей ветки.

События и реальные решения дописываются в конец без извлечения фактов,
числовых реестров, сжатия или включения художественной прозы в промпты.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
import json
from dataclasses import dataclass, field, asdict
from scene_locations import load_places, place_id, ensure_current_place
from scene_conditions import initial_conditions, advance_conditions
from character_identity import next_id, valid_id, resolve_ref, migrate_state


MIN_CHARACTER_AGE = 18


class AgeValidationError(Exception):
    """Возраст персонажа отсутствует или имеет неверный формат."""
    pass


@dataclass
class Character:
    name: str
    age: int
    role: str = ""
    traits: str = ""  # характер — внутреннее: привычки, манера речи, мотивация
    appearance: str = ""  # внешность — внешнее: телосложение, одежда, деталь.
    # Разделены намеренно: одно про то, каким персонаж выглядит физически,
    # другое про то, как он себя ведёт и говорит.
    # Персонаж существует в state.characters с момента кастинга — ДО того,
    # как игрок прочитал хоть одну сцену (весь каст создаётся заранее).
    # revealed — отдельный флаг именно того, что игрок РЕАЛЬНО увидел этого
    # персонажа в прочитанной сцене (заговорил в ней) — для панели
    # персонажей: неоткрытым показываем только имя, открытым — всё досье.
    revealed: bool = False

    # Ровно один персонаж во всём касте — тот, за кого играет игрок.
    # Раньше это решалось расплывчато ("один из них обычно протагонист"),
    # теперь явное поле — нужно, чтобы фронт точно знал, кого показывать
    # на стартовом экране в стиле Half-Life (см. is_protagonist в промпте
    # каста). Защита от того, что модель пометит 0 или больше 1 —
    # в generate_characters, не здесь.
    is_protagonist: bool = False

    # Internal narrative functions must never be exposed in the notebook.
    story_role: str = ""
    story_purpose: str = ""
    appearance_features: dict[str, str] = field(default_factory=dict)
    id: str = ""  # stable within this story; display names may repeat
    sprite_id: str = ""
    sprite_appearance: str = ""
    kind: str = "human"  # nonhuman uses the existing procedural sphere

    def __post_init__(self):
        # Возраст нормализуется до попадания в каст, промпты и сохранения.
        # Отсутствующее/нечисловое значение остаётся ошибкой формата.
        try:
            if isinstance(self.age, bool):
                raise ValueError('boolean age')
            age = int(self.age)
        except (TypeError, ValueError, OverflowError) as exc:
            raise AgeValidationError(
                f"Персонаж '{self.name}': возраст не указан или не является числом."
            ) from exc
        self.age = max(MIN_CHARACTER_AGE, age)
        self.kind = self.kind if self.kind in {"human", "nonhuman"} else "human"


@dataclass
class StoryState:
    genre: str
    tone: str
    target_length: int          # общее число сцен, к которому ведём историю
    current_scene: int = 0
    characters: dict[str, Character] = field(default_factory=dict)  # keyed by c1, c2, ...
    sprite_seed: str = field(default_factory=lambda: uuid.uuid4().hex)
    character_expressions: dict[str, str] = field(default_factory=dict)
    event_history: list[dict] = field(default_factory=list)  # append-only raw events/choices

    # Сколько сцен реально показано игроку. Отдельно от current_scene:
    # тот растёт СКАЧКАМИ при материализации (весь сегмент применяется
    # разом, +17 за раз), а этот — по одной на каждую реально доставленную
    # сцену. Без него в транскрипте все сцены сегмента получали одинаковый
    # номер — конечный номер сегмента.
    delivered_scenes: int = 0

    recent_lines: list[dict] = field(default_factory=list)  # последние N реплик
    player_choices: list[str] = field(default_factory=list)  # все реальные выборы/действия героя
    location: str = ""
    location_id: str = ""
    locations: dict[str, dict] = field(default_factory=dict)
    # Постоянные время и погода плюс локальные запросы последней сцены.
    # Пропущенные поля сохраняют условия; частицы и события не наследуются.
    scene_conditions: dict = field(default_factory=dict)

    # One-time high-level outline: ordered segments with places/events,
    # world rules, timeline and initial conditions. Legacy phase plans load too.
    story_plan: dict | None = None

    # Связи МЕЖДУ персонажами — список пар, не мнение каждого по отдельности.
    # {"between": [id1, id2], "type": "...", "attitude": "..."}. Одна
    # общая связь на пару, а не два разных текста (у Алекса своё мнение про
    # Марию, у Марии своё) — иначе они рассинхронизировались бы между собой,
    # и сцены получили бы противоречивую картину одних и тех же отношений.
    character_relationships: list[dict] = field(default_factory=list)

    # Неисполненные исходные события/решения перед отложенным продолжением.
    # После посещения они попадают в event_history и не дублируются в промпте.
    planned_events_so_far: list[str] = field(default_factory=list)
    segment_index: int = 0  # zero-based outline slot; independent of exact scene counts
    pending_choice: str | None = None
    branch_path: list[str] = field(default_factory=list)

    # Куда игрок попросил выгружать диагностический лог — задаётся один раз
    # при старте (см. /story/start), но нужен ещё и позже, во время игры:
    # ленивая материализация прозы (_materialize_segment в server.py)
    # происходит в момент, когда игрок реально доходит до сегмента, а не
    # только на старте — исходный запрос StartRequest к этому моменту давно
    # недоступен, значит путь должен пережить всю сессию на самом состоянии.
    log_path: str | None = None

    # Обычный режим готовит события двух ближайших альтернатив;
    # экономичный — только выбранное продолжение.
    build_all_branches: bool = True
    # Incremental tree with pending continuations and reusable generated segments.
    # Плоская карта id -> узел, а не
    # вложенная структура — проще сохранять (обычный dict, без рекурсии при
    # json.dumps) и переходить между узлами за O(1) по id, а не спускаться
    # от корня. Узел: {"scene": scene_dict, "children": {choice_text: child_id}}.
    story_tree: dict[str, dict] = field(default_factory=dict)
    tree_root_id: str | None = None
    tree_current_id: str | None = None  # None = дерево ещё не построено
    # После сборки дерева tree_current_id сразу указывает на корень, но сам
    # игрок его ещё не видел — первый запрос сцены должен просто отдать
    # текущий узел как есть, а не пытаться "перейти" по req.choice (там
    # ещё нет предыдущей показанной сцены, от которой был бы сделан выбор).
    tree_first_delivered: bool = False

    # 24, не 15 — с тех пор как narration тоже пишется в recent_lines (не
    # только диалог), на сцену уходит до 2 записей вместо 1. Подняли лимит,
    # чтобы эффективное окно "сколько сцен реально видно" не сократилось
    # вдвое при том же значении.
    RECENT_LINES_LIMIT = 24


    # Учёт расхода — из поля usage реальных ответов DeepSeek, не по оценке.
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_api_calls: int = 0
    # Кэш промпта (DeepSeek context caching). cache_hit — токены префикса,
    # совпавшие с предыдущим запросом и посчитанные в 50 раз дешевле
    # (Flash: $0.003/1M против $0.15/1M). Нужны, чтобы измерять реальную
    # экономию: доля cache_hit / (cache_hit + cache_miss) по всей истории.
    total_cache_hit_tokens: int = 0
    total_cache_miss_tokens: int = 0

    # ---------- Мутация состояния ----------

    def add_character(
        self, name: str, age: int, role: str = "", traits: str = "", appearance: str = "",
        is_protagonist: bool = False, story_role: str = "", story_purpose: str = "",
        appearance_features: dict | None = None, character_id: str | None = None,
        kind: str = "human",
    ) -> Character:
        """
        Единственная точка входа для новых персонажей.
        Числовой возраст ниже 18 повышается до 18. Отсутствующий или
        нечисловой возраст вызывает AgeValidationError.
        """
        # Безымянный персонаж попал бы в каст, в промпт и в сверку имён,
        # ломая всё три. Молча пропускаем — это данные от модели, отдельного
        # исключения они не заслуживают, но и в состояние им нельзя.
        name = (name or "").strip()
        if not name:
            raise ValueError("Персонаж без имени не может быть добавлен")

        cid = character_id if valid_id(character_id) else next_id(self.characters)
        if cid in self.characters:
            raise ValueError(f"Повторный ID персонажа: {cid}")
        char = Character(
            id=cid, name=name, age=age, role=role, traits=traits, appearance=appearance,
            is_protagonist=is_protagonist, story_role=story_role, story_purpose=story_purpose,
            appearance_features=copy.deepcopy(appearance_features) if isinstance(appearance_features, dict) else {},
            kind=kind,
        )
        self.characters[cid] = char
        return char

    def add_context(self, text: str) -> None:
        """Keep initial input or imported legacy context without creating facts."""
        if isinstance(text, str) and text.strip():
            self.event_history.append({"context": text.strip()})

    def record_event(self, event, *, legacy_changes: dict | None = None) -> None:
        """Archive the source event only after its scene was successfully applied."""
        if not isinstance(event, (dict, str)) or not event:
            return
        record = {"scene": self.current_scene + 1, "event": copy.deepcopy(event)}
        if legacy_changes:
            record["legacy_changes"] = copy.deepcopy(legacy_changes)
        self.event_history.append(record)

    def memory_prompt_block(self) -> str:
        header = (
            "[СВЕРШИВШИЕСЯ СОБЫТИЯ — ПОЛНЫЙ ИСХОДНЫЙ ЖУРНАЛ ТЕКУЩЕЙ ВЕТКИ]\n"
            "Каждая строка — JSON: scene — номер сцены, event — исходное событие, "
            "choice — реальное решение игрока, context — исходные условия/данные старого сохранения. "
            "Учитывай весь журнал по порядку, включая точные числа и сроки внутри событий. "
            "Изменения более поздних событий действуют дальше, не отменяя прошлого. "
            "Будущие планы ниже не являются произошедшими событиями. "
            "Художественный текст прошлых сцен не передаётся.\n"
        )
        return header + "".join(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")) + "\n"
                                for record in self.event_history)

    def record_usage(self, usage: dict | None):
        """
        Учёт реального расхода токенов из поля usage ответа DeepSeek —
        не оценка по словам, а точные цифры прямо из API.
        Принимает None молча (например, если ответ по какой-то причине
        пришёл без usage) — учёт не должен ронять основной вызов.
        """
        if not isinstance(usage, dict) or not usage:
            return

        def as_count(value) -> int:
            """
            Строка вместо числа роняла учёт TypeError'ом, а отрицательное
            значение уводило счётчик в минус — и то и другое приходит извне,
            от API, так что полагаться на формат нельзя. Учёт расхода не
            должен ронять уже полученную сцену.
            """
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        self.total_input_tokens += as_count(usage.get("prompt_tokens"))
        self.total_output_tokens += as_count(usage.get("completion_tokens"))
        self.total_cache_hit_tokens += as_count(usage.get("prompt_cache_hit_tokens"))
        self.total_cache_miss_tokens += as_count(usage.get("prompt_cache_miss_tokens"))
        self.total_api_calls += 1

    def add_line(self, speaker: str, text: str, *, resolve: bool = True):
        cid = resolve_ref(self.characters, speaker) if resolve else None
        label = self.characters[cid].name if cid else speaker
        self.recent_lines.append({"speaker_id": cid, "speaker": label, "text": text})
        if len(self.recent_lines) > self.RECENT_LINES_LIMIT:
            self.recent_lines.pop(0)

    def add_player_choice(self, choice: str):
        """
        Сохраняет все реальные решения без обобщения.
        """
        value = str(choice or "").strip()
        if not value or value.casefold() == "продолжить" or value.startswith("Начни историю с завязки:"):
            return
        self.player_choices.append(value)
        self.event_history.append({"scene": self.current_scene + 1, "choice": value})

    def advance_scene(self):
        self.current_scene += 1

    def touch_character(self, name: str):
        """Раскрывает присутствующего персонажа для блокнота."""
        cid = resolve_ref(self.characters, name)
        char = self.characters.get(cid)
        if char:
            char.revealed = True

    def character_id(self, ref: str) -> str | None:
        """Resolve an ID or an unambiguous legacy display name."""
        return resolve_ref(self.characters, ref)

    # ---------- Сериализация в промпт ----------

    @property
    def progress_ratio(self) -> float:
        read = self.delivered_scenes or self.current_scene
        return min(read / self.target_length, 1.0) if self.target_length > 0 else 0.0

    def characters_block(self) -> str:
        """
        Полный, НЕИЗМЕННЫЙ на всю историю состав каста и связей между
        персонажами — решается один раз на кастинге, до всякой прозы, и
        больше не меняется. Специально не фильтрует активных/неактивных
        — весь состав целиком, чтобы этот текст можно было положить
        в кэшируемый системный промпт (см. build_scene_system_prompt) и
        не пересчитывать/не пересобирать заново на каждый вызов истории.
        """
        chars = "\n".join(
            f"  - {c.id}: {c.name} ({c.age} лет, {c.role}): {c.sprite_appearance or c.appearance}. Характер: {c.traits}. "
            f"Пол: {c.appearance_features.get('gender', 'unknown')}. ГГ: {c.is_protagonist}. "
            f"Тип: {c.kind}. Функция: {c.story_role}. Задача в истории: {c.story_purpose}"
            for c in self.characters.values()
        ) or "  (пока нет)"

        relationships_block = ""
        if self.character_relationships:
            lines = "\n".join(
                f"  - {r.get('between', ['?', '?'])[0]} ↔ {r.get('between', ['?', '?'])[1]}: "
                f"{r.get('type', '')}, отношение — {r.get('attitude', '')}"
                for r in self.character_relationships
                if isinstance(r, dict) and isinstance(r.get("between"), list) and len(r["between"]) == 2
            )
            if lines:
                relationships_block = f"\n\nСвязи между персонажами:\n{lines}"

        return f"""

[КАСТ ИСТОРИИ — фиксирован, не меняется]
Персонажи (все совершеннолетние). ID постоянны: используй их в participants,
actor, speaker_id и связях. В happening.subject/object и entities.holder также
используй ID, если значение — персонаж; иначе обычное описание предмета/явления.
Имена нужны для художественного текста. Одинаковое имя не означает одного человека.
Старые события могут содержать имена вместо ID; не создавай по ним новых персонажей:

{chars}{relationships_block}
"""

    def to_prompt_block(self, include_memory: bool = True) -> str:
        """Only generation position/decision; current state is derived from events."""
        memory = self.memory_prompt_block() + "\n" if include_memory else ""
        choice = ("\n[ПРИНЯТОЕ РЕШЕНИЕ ПЕРЕД ЭТИМ СЕГМЕНТОМ]\n" + self.pending_choice
                  if self.pending_choice else "")
        return memory + (
            f"[ПОЗИЦИЯ ГЕНЕРАЦИИ]\nСцена {self.current_scene} из ~{self.target_length}. "
            f"Сегмент {self.segment_index + 1}.\n"
            "Текущее место, условия, предметы и незакрытые вопросы определяй по "
            "исходным условиям и событиям по порядку. Более позднее изменение действует дальше."
            + choice
        )

    def to_events_prompt_block(self, include_memory: bool = True) -> str:
        """
        Облегчённая версия to_prompt_block для этапа ПЛАНИРОВАНИЯ событий
        (см. planned_events_so_far выше) — персонажи и связи между ними
        теперь в characters_block() (кэшируемая часть системного
        сообщения), здесь остаётся только динамика: сцена и уже решённые
        события этой ветки. Фактов/реплик ещё нет: для ещё не написанных
        прозой веток вместо них — цепочка уже принятых решений о том, что
        произойдёт, компактно, не полным текстом.
        """
        # Already accepted sources are in the cached journal, not repeated as plans.
        from story_events import event_prompt
        accepted = [event_prompt(record["event"]) if "event" in record
                    else f"[Выбор героя] {record['choice']}"
                    for record in self.event_history if "event" in record or "choice" in record]
        offset = 0
        while (offset < min(len(accepted), len(self.planned_events_so_far))
               and accepted[offset] == self.planned_events_so_far[offset]):
            offset += 1
        remaining_plan = self.planned_events_so_far[offset:]
        if self.pending_choice and remaining_plan[-1:] == [f"[Выбор героя] {self.pending_choice}"]:
            remaining_plan = remaining_plan[:-1]
        events = (
            "\n".join(f"  {i+1}. {e}" for i, e in enumerate(remaining_plan))
            or "  (история ещё не начиналась)"
        )

        future = (
            "\n\n[ПРЕДШЕСТВУЮЩИЙ ПЛАН ЭТОГО ПУТИ]\n"
            "Ещё не свершившиеся события/решение, необходимые для подготовки продолжения:\n"
            + events
        ) if remaining_plan else ""
        return self.to_prompt_block(include_memory=include_memory) + future

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str):
        from storage import atomic_json
        atomic_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str) -> "StoryState":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "StoryState":
        """
        Собирает состояние из словаря. Отделено от load(), потому что
        нужно не только при чтении файла: отложенная ветка дерева хранит
        снимок состояния именно словарём (объект не переживёт JSON и
        round-trip), и при достройке восстанавливается этим же путём.
        """
        data = migrate_state(data)  # Includes legacy tree scenes/deferred snapshots.
        state = cls(
            genre=data["genre"],
            tone=data["tone"],
            target_length=data["target_length"],
            current_scene=data["current_scene"],
            event_history=data.get("event_history", []),
            sprite_seed=data.get("sprite_seed") or hashlib.sha256(json.dumps(data.get("characters", {}), ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            character_expressions=data.get("character_expressions", {}),
            delivered_scenes=data.get("delivered_scenes", 0),
            recent_lines=data["recent_lines"],
            player_choices=data.get("player_choices", []),  # старые сохранения без этого поля
            location=data["location"],
            location_id=place_id(data.get("location_id")),
            locations=load_places(data.get("locations")),
            scene_conditions=advance_conditions(initial_conditions(data.get("story_plan")), data.get("scene_conditions")),
            total_input_tokens=data.get("total_input_tokens", 0),
            total_output_tokens=data.get("total_output_tokens", 0),
            total_api_calls=data.get("total_api_calls", 0),
            total_cache_hit_tokens=data.get("total_cache_hit_tokens", 0),
            total_cache_miss_tokens=data.get("total_cache_miss_tokens", 0),
            story_plan=data.get("story_plan"),
            character_relationships=data.get("character_relationships", []),
            planned_events_so_far=data.get("planned_events_so_far", []),
            segment_index=data.get("segment_index", 0),
            pending_choice=data.get("pending_choice"),
            branch_path=data.get("branch_path", []),
            log_path=data.get("log_path"),
            build_all_branches=data.get("build_all_branches", True),
            story_tree=data.get("story_tree", {}),
            tree_root_id=data.get("tree_root_id"),
            tree_current_id=data.get("tree_current_id"),
            tree_first_delivered=data.get("tree_first_delivered", False),
        )
        if "event_history" not in data:
            legacy = {key: data[key] for key in
                      ("memory_log", "facts", "numeric_facts", "player_choices", "entities",
                       "resources", "world_rules", "open_threads", "scene_conditions",
                       "location", "location_id", "locations") if data.get(key)}
            if legacy:
                state.event_history.append({"context": {"legacy_state": legacy}})
        for cid, c in data["characters"].items():
            state.characters[cid] = Character(**{key: value for key, value in c.items()
                                                  if key in Character.__dataclass_fields__})
        ensure_current_place(state)
        return state
