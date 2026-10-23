import { useCallback, useEffect, useRef, useState } from "react";
import { PreferencesProvider, PreferenceControls, usePreferences } from './Preferences.jsx';
import FallbackLog, { reportArtFailure } from './FallbackLog.jsx';
import { useReader } from './useReader.js';
import { interactiveTarget, speakerColor } from './reader.js';
import SceneCharacters from "./SceneCharacters.jsx";
import SceneBackground from "./SceneBackground.jsx";
import { backgroundTime } from "./backgroundProcessing.js";
import SceneAmbience from "./SceneAmbience.jsx";
import SceneMusic from "./SceneMusic.jsx";
import { browserStorage } from "./browserStorage.js";
import { apiFetch as fetch, apiError } from "./api.js";
import { MenuFrame, MenuProvider, useMenuTime } from "./MenuAppearance.jsx";
import PremiseSuggestions, { usePremiseSuggestions } from "./PremiseSuggestions.jsx";

// API и ассеты используют origin страницы; в разработке работает Vite proxy.
const API = "";

const GENRES = [
  "детектив",
  "фэнтези",
  "sci-fi",
  "хоррор",
  "драма",
  "приключение",
  "мистика",
  "производственный роман",
];

const TONES = ["мрачный", "лёгкий", "загадочный", "ироничный", "меланхоличный", "напряжённый"];

// Механизм окон выбора считается по процентам от длины (см. choice_windows
// на бэкенде) — работает для любой длины без правок бэкенда, поэтому выбор
// можно вернуть безопасно.
const LENGTHS = [
  { label: "Средняя", scenes: 47, note: "~30 минут чтения" },
  { label: "Длинная", scenes: 95, note: "~1 час чтения" },
];

// ID последней истории; сам снимок хранится на диске сервера.
const RESUME_KEY = "story_engine_session";

/* Корешок: засечка на каждую сцену, заполняется к финалу.
   При длинных историях сжимаем до 24 засечек, чтобы не превратить в частокол. */
function Spine({ current, total }) {
  const shown = Math.min(total, 24);
  const scale = total / shown;
  const positions = Array.from({ length: shown }, (_, i) => Math.round((i + 1) * scale));
  /* Засечка «здесь вы сейчас» — БЛИЖАЙШАЯ к текущей сцене, не точное
     совпадение. При сжатии 47 сцен в 24 засечки они встают на чётные
     номера, и на нечётной сцене точное сравнение не находило ни одной —
     текущая позиция просто пропадала с корешка. */
  let currentIdx = 0;
  let bestDist = Infinity;
  positions.forEach((at, i) => {
    const d = Math.abs(at - current);
    if (d < bestDist) {
      bestDist = d;
      currentIdx = i;
    }
  });
  const notches = positions.map((at, i) => {
    if (i === currentIdx) return "current";
    return at < current ? "done" : "todo";
  });

  // Засечек всего 24 на историю в 47 сцен — одна засечка равна двум сценам,
  // и половину ходов полоска стоит на месте. Заполнение самой нити считается
  // по точному прогрессу и двигается на КАЖДОЙ сцене.
  const filled = total > 0 ? Math.min(100, (current / total) * 100) : 0;

  return (
    <aside className="spine" aria-label={`Сцена ${current} из ${total}`}>
      <div className="spine__thread">
        <div className="spine__thread-fill" style={{ height: `${filled}%` }} />
      </div>
      <div className="spine__notches">
        {notches.map((kind, i) => (
          <div key={i} className={`notch ${kind === "todo" ? "" : `notch--${kind}`}`} />
        ))}
      </div>
      <div className="spine__label">
        {String(current).padStart(2, "0")} / {total}
      </div>
    </aside>
  );
}

/* Shared API, audio, typography and missing-art preferences. */
function Settings({ status, onSave, onSkip, saving, canSkip, error, time, overlay = false }) {
  const focusRef = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    focusRef.current?.focus();
    return () => previous?.focus?.();
  }, []);
  function trap(event) {
    if (event.key === 'Escape' && canSkip) { event.preventDefault(); onSkip(); }
    if (event.key === 'Tab') {
      const nodes = [...focusRef.current.querySelectorAll('button:not(:disabled),a[href],input,select,textarea')];
      const first = nodes[0], last = nodes.at(-1);
      if (event.shiftKey && (document.activeElement === first || document.activeElement === focusRef.current)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }
  const [deepseek, setDeepseek] = useState("");

  const hasAnyInput = Boolean(deepseek);

  return (
    <div ref={focusRef} tabIndex={-1} onKeyDown={trap} className={overlay ? "settings-overlay" : "settings-screen"} role={overlay ? "dialog" : undefined} aria-modal={overlay ? true : undefined} aria-label="Настройки">
    <MenuFrame time={time} overlay={overlay}>
      <div className="setup__card">
        <p className="eyebrow">Настройка</p>
        <h1 className="setup__title">Настройки</h1>
        <PreferenceControls />
        <FallbackLog />
        <h2>Ключ DeepSeek</h2>
        <p className="setup__lede">
          Ключ хранится локально на этой машине и используется только для
          обращения к DeepSeek — никуда больше не уходит.
        </p>

        <div className="field">
          <label className="field__label" htmlFor="ds-key">
            DeepSeek {status.deepseek_api_key && <span className="key-status">— задан</span>}
          </label>
          <input
            id="ds-key"
            type="password"
            className="field__input"
            value={deepseek}
            onChange={(e) => setDeepseek(e.target.value)}
            placeholder={status.deepseek_api_key ? "•••••••••• (оставь пустым, чтобы не менять)" : "sk-..."}
            autoComplete="off"
          />
        </div>

        {error && <div className="notice">{error}</div>}

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <button
            className="btn"
            disabled={saving || !hasAnyInput}
            onClick={() =>
              onSave({
                deepseek_api_key: deepseek || undefined,
              })
            }
          >
            {saving ? "Сохраняем…" : "Сохранить"}
          </button>
          {canSkip && (
            <button className="btn btn--ghost" onClick={onSkip}>
              Вернуться
            </button>
          )}
        </div>
      </div>
    </MenuFrame>
    </div>
  );
}

/* Список сохранённых историй — показывается перед онбордингом, если есть
   хотя бы одна. Продолжение грузит сцену как есть, без новой генерации;
   "Новая история" ведёт в обычный онбординг, список никуда не девается. */
function ResumePointsPicker({ info, onPick, onCancel, busy, error }) {
  return (
    <MenuFrame>
      <div className="setup__card">
        <p className="eyebrow">Загружено из файла</p>
        <h1 className="setup__title">
          Откуда <em>продолжить</em>?
        </h1>
        <p className="setup__lede">
          {info.genre} · {info.tone} · {info.target_length} сцен
        </p>

        {error && <div className="notice">{error}</div>}

        <div className="chips" style={{ flexDirection: "column", alignItems: "stretch" }}>
          {info.resume_points.map((rp) => (
            <button
              key={rp.node_id}
              className="chip"
              disabled={busy}
              onClick={() => onPick(rp.node_id)}
              style={{ textAlign: "left", marginBottom: 8 }}
            >
              {rp.label}
            </button>
          ))}
        </div>

        <button className="btn btn--ghost" style={{ marginTop: 24 }} onClick={onCancel} disabled={busy}>
          Начать новую историю вместо этого
        </button>
      </div>
    </MenuFrame>
  );
}

function ResumePrompt({ info, onResume, onDiscard }) {
  const st = info.state ?? {};
  return (
    <MenuFrame>
      <div className="setup__card">
        <p className="eyebrow">Незаконченная история</p>
        <h1 className="setup__title">
          Продолжить <em>с места</em>?
        </h1>
        <p className="setup__lede">
          {st.genre} · {st.tone} · сцена {st.current_scene ?? "?"} из {st.target_length ?? "?"}
        </p>
        {info.scene?.narration && (
          <p className="prose prose--narration" style={{ marginTop: 16 }}>
            {info.scene.narration.slice(0, 220)}…
          </p>
        )}
        <button className="btn" style={{ marginTop: 24 }} onClick={onResume}>
          Продолжить
        </button>
        <button className="btn btn--ghost" style={{ marginTop: 12 }} onClick={onDiscard}>
          Начать новую историю
        </button>
      </div>
    </MenuFrame>
  );
}

function ChoiceJournal({ state, open, onToggle }) {
  const made = state?.player_choices ?? [];
  if (!made.length) return null;
  return (
    <div className={`journal ${open ? "journal--open" : ""}`}>
      <button className="journal__tab" onClick={onToggle}>
        {open ? "Закрыть" : "Мои решения"}
      </button>
      {open && (
        <div className="journal__page">
          {made.map((c, i) => (
            <div key={i} className="journal__entry">
              <span className="journal__num">{String(i + 1).padStart(2, "0")}</span>
              <span>{c}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Setup({ onStart, onClarify, busy, error, onLoadJsonl }) {
  const suggestions = usePremiseSuggestions();
  const [step, setStep] = useState("seed"); // seed → details → clarify
  const [seed, setSeed] = useState("");
  const [questions, setQuestions] = useState([]);
  const [answers, setAnswers] = useState({});
  const [genre, setGenre] = useState("детектив");
  const [tone, setTone] = useState("мрачный");
  const [logPath, setLogPath] = useState("");
  const [length, setLength] = useState(LENGTHS[0]);
  // Обычный режим готовит события двух ближайших продолжений.
  // Экономный генерирует события только после решения игрока.
  const [buildAllBranches, setBuildAllBranches] = useState(true);

  // Сужаются под конкретную затравку при переходе на шаг details — по
  // умолчанию полные списки (на случай сбоя фильтрации ничего не теряем,
  // просто увидим все варианты, как и было раньше).
  const [availableGenres, setAvailableGenres] = useState(GENRES);
  const [availableTones, setAvailableTones] = useState(TONES);
  const [fittingGenreTone, setFittingGenreTone] = useState(false);
  const [seedError, setSeedError] = useState(null);

  // Сужает жанры/тоны под конкретную затравку перед показом шага деталей —
  // если из текста уже ясно, что это хоррор, незачем предлагать "лёгкий"
  // тон наравне с остальными. Отдельной модерации затравки нет.
  const proceedToDetails = async () => {
    setFittingGenreTone(true);
    setSeedError(null);
    try {
      const r = await fetch(`${API}/story/genre_tone_fit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seed: seed.trim(), genre }),
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        setSeedError(data.detail?.message || "Не удалось подобрать жанр и тон. Попробуй ещё раз.");
        return;
      }
      if (r.ok) {
        const data = await r.json();
        if (data.genres?.length) {
          setAvailableGenres(data.genres);
          if (!data.genres.includes(genre)) setGenre(data.genres[0]);
        }
        if (data.tones?.length) {
          setAvailableTones(data.tones);
          if (!data.tones.includes(tone)) setTone(data.tones[0]);
        }
      }
      setStep("details");
    } catch {
      setSeedError("Бэкенд не отвечает. Завязка пока не проверена.");
    } finally {
      setFittingGenreTone(false);
    }
  };

  // Жанр/тон теперь известны ДО уточняющих вопросов (порядок шагов
  // изменился: seed → details → clarify) — вопросы строятся конкретно под
  // уже выбранный жанр, а не абстрактно про тон, который к этому моменту
  // уже выбран. Затравка передаётся без отдельной модерации.
  const submitDetails = async () => {
    const qs = await onClarify(seed.trim(), genre, tone);
    if (qs === null) {
      setStep("seed"); // не удалось получить уточняющие вопросы
      return;
    }
    setQuestions(qs);
    setStep(qs.length ? "clarify" : "start");
  };

  const pickAnswer = (qIndex, option) => {
    setAnswers((a) => ({ ...a, [qIndex]: option }));
  };

  const allAnswered = questions.length > 0 && questions.every((_, i) => answers[i]);

  const startPayload = () => ({
    seed: seed.trim(),
    genre,
    tone,
    target_length: length.scenes,
    clarifications: questions.map((q, i) => `${q.question} — ${answers[i]}`),
    log_path: logPath.trim() || null,
    build_all_branches: buildAllBranches,
  });

  if (step === "seed") {
    return (
      <MenuFrame>
        <div className="setup__card">
          <p className="eyebrow">Движок историй</p>
          <h1 className="setup__title">
            С чего всё <em>начинается</em>?
          </h1>
          <p className="setup__lede">
            Опиши завязку в двух словах — дальше движок сам уточнит детали
            и предложит собрать историю вокруг них.
          </p>

          <PremiseSuggestions seed={seed} onPick={setSeed} suggestions={suggestions} />

          <div className="field">
            <label className="field__label" htmlFor="seed">
              Завязка
            </label>
            <textarea
              id="seed"
              className="field__input field__input--premise"
              rows={3}
              value={seed}
              onChange={(e) => setSeed(e.target.value)}
              placeholder="Смотритель маяка получает письмо от того, кто утонул год назад"
              maxLength={2000}
            />
          </div>

          {error && <div className="notice">{error}</div>}

          <button className="btn" disabled={!seed.trim() || fittingGenreTone} onClick={proceedToDetails}>
            {fittingGenreTone ? "Проверяем завязку и подбираем варианты…" : "Дальше"}
          </button>

          <label className="btn btn--ghost" style={{ marginTop: 12, display: "block", textAlign: "center" }}>
            Загрузить историю из файла (.jsonl)
            <input
              type="file"
              accept=".jsonl"
              style={{ display: "none" }}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) onLoadJsonl(file);
                e.target.value = ""; // тот же файл можно выбрать повторно
              }}
            />
          </label>
          <p className="field__hint" style={{ marginTop: 6 }}>
            Нужен <strong>диагностический</strong> лог (<code>*_debug.jsonl</code>) — тот,
            что содержит план, каст и события сцен. Игровой протокол истории
            (<code>*_transcript.jsonl</code>) для восстановления не подходит: в нём
            только тексты сцен.
          </p>
        </div>
      </MenuFrame>
    );
  }

  if (step === "details") {
    return (
      <MenuFrame>
        <div className="setup__card">
          <p className="eyebrow">Каким будет мир</p>
          <h1 className="setup__title">Жанр, тон, длина</h1>
          <p className="setup__lede">
            У истории будет конец: выбери, насколько далеко он стоит.
          </p>

          <div className="field">
            <span className="field__label">Жанр</span>
            <div className="chips">
              {availableGenres.map((g) => (
                <button
                  key={g}
                  className={`chip ${g === genre ? "chip--on" : ""}`}
                  onClick={() => setGenre(g)}
                >
                  {g}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <span className="field__label">Тон</span>
            <div className="chips">
              {availableTones.map((t) => (
                <button
                  key={t}
                  className={`chip ${t === tone ? "chip--on" : ""}`}
                  onClick={() => setTone(t)}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <span className="field__label">Длина</span>
            <div className="chips">
              {LENGTHS.map((l) => (
                <button
                  key={l.label}
                  className={`chip ${l.scenes === length.scenes ? "chip--on" : ""}`}
                  onClick={() => setLength(l)}
                >
                  {l.label} · {l.note}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <span className="field__label">Модель</span>
            <p className="field__hint">DeepSeek V4.1 Flash</p>
          </div>

          <div className="field">
            <span className="field__label">Полнота</span>
            <button
              className={`chip ${buildAllBranches ? "chip--on" : ""}`}
              onClick={() => setBuildAllBranches((v) => !v)}
            >
              {buildAllBranches
                ? "Два ближайших продолжения и своё действие"
                : "Только выбранный путь — дешевле"}
            </button>
            <p className="field__hint">
              {buildAllBranches
                ? "Готовим события двух ближайших продолжений, без глубоких веток. Текст пишется после выбора; можно предложить своё действие."
                : "До выбора альтернативы не генерируются. Пишем только выбранное продолжение — после решения потребуется подождать. Своё действие недоступно."}
            </p>
          </div>

          {error && <div className="notice">{error}</div>}

          <button className="btn" disabled={busy} onClick={submitDetails}>
            {busy ? "Обдумываем…" : "Дальше"}
          </button>
        </div>
      </MenuFrame>
    );
  }

  if (step === "clarify") {
    return (
      <MenuFrame>
        <div className="setup__card">
          <p className="eyebrow">Пара уточнений</p>
          <h1 className="setup__title">Как рассказать эту историю?</h1>
          <p className="setup__lede">Выбери, что ближе — детали именно этого сюжета.</p>

          {questions.map((q, i) => (
            <div className="field" key={i}>
              <span className="field__label">{q.question}</span>
              <div className="chips">
                {q.options.map((opt) => (
                  <button
                    key={opt}
                    className={`chip ${answers[i] === opt ? "chip--on" : ""}`}
                    onClick={() => pickAnswer(i, opt)}
                  >
                    {opt}
                  </button>
                ))}
              </div>
            </div>
          ))}

          <button className="btn" disabled={!allAnswered} onClick={() => setStep("start")}>
            Дальше
          </button>
        </div>
      </MenuFrame>
    );
  }

  return (
    <MenuFrame>
      <div className="setup__card">
        <p className="eyebrow">Готово</p>
        <h1 className="setup__title">Начинаем?</h1>
        <p className="setup__lede">
          Дальше — общий план по сегментам, персонажи и события первого сегмента. История раскрывается по мере прохождения.
        </p>

        <div className="field">
          <label className="field__label" htmlFor="logPath">
            Куда выгружать диагностический лог (папка на диске сервера)
          </label>
          <input
            id="logPath"
            className="field__input"
            value={logPath}
            onChange={(e) => setLogPath(e.target.value)}
            placeholder="/home/user/story_logs (пусто — папка по умолчанию)"
          />
        </div>

        {error && <div className="notice">{error}</div>}

        <button className="btn" disabled={busy} onClick={() => onStart(startPayload())}>
          {busy ? "Собираем историю…" : "Начать историю"}
        </button>
      </div>
    </MenuFrame>
  );
}

/* Заголовок финального экрана зависит от того, как именно закончилась
   история — обычное завершение по длине не то же самое, что гибель
   персонажа посреди сюжета. Сам текст причины — в narration, это только
   короткая подпись сверху. */
function finaleTitle(ending) {
  switch (ending) {
    case "death":
      return "Герой погиб";
    case "failure":
      return "Провал";
    case "victory":
      return "Победа";
    default:
      // Сюда же попадает служебное "complete", которое бэкенд ставит на
      // финальную сцену, если модель не указала характер концовки сама —
      // нейтральный заголовок тут и есть правильный ответ.
      return "Конец истории";
  }
}

/* Минималистичный "блокнот" справа снизу — сворачиваемый, чуть анимированный.
   Персонажи уже отфильтрованы бэкендом (_public_state): нераскрытым отдано
   только имя, раскрытым — полное досье. Протагонист виден с первого
   момента (backend всегда помечает revealed=true для него) — это тоже
   часть блокнота, не отдельная сущность. */
function CharacterNotebook({ state, open, onToggle }) {
  const characters = state?.characters ?? [];

  if (!characters.length) return null;

  return (
    <div className={`notebook ${open ? "notebook--open" : ""}`}>
      <button className="notebook__tab" onClick={onToggle}>
        {open ? "Закрыть" : "Персонажи"}
      </button>
      {open && (
        <div className="notebook__page">
          {characters.map((c) => (
            <div key={c.id ?? c.name} className="notebook__entry">
              <div className="notebook__name">
                {c.name}
                {c.is_protagonist && <span className="notebook__you"> (вы)</span>}
              </div>
              {c.is_protagonist ? (
                <>
                  <div className="notebook__role">{c.role}</div>
                  <div className="notebook__detail">Внешность: {c.appearance}</div>
                  <div className="notebook__detail">Характер: {c.traits}</div>
                </>
              ) : c.revealed ? (
                <><div className="notebook__detail">Внешность: {c.appearance || 'не описана'}</div>
                <div className="notebook__detail">
                  Отношение к вам: {c.relation_type || "неясно"}, {c.relation_attitude || "неясно"}
                </div></>
              ) : (
                <div className="notebook__unknown" title="пока неизвестно" />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* Разовый экран в начале игры — кто протагонист, за кого играем. Показан
   один раз (см. shownIntro в Stage ниже), не всплывающая подпись на каждого
   персонажа по ходу игры, а именно вступление про самого игрока. */
function ProtagonistIntro({ protagonist, onDismiss }) {
  // Фокус ставим сразу: tabIndex делает оверлей достижимым с клавиатуры,
  // но без фокуса нажатия уходили бы в то, что под ним.
  const ref = useRef(null);
  useEffect(() => {
    ref.current?.focus();
  }, []);

  return (
    /* role/tabIndex/onKeyDown — потому что это полноэкранный оверлей,
       перекрывающий всю игру. Без клавиатурного выхода тот, кто играет
       без мыши, просто не смог бы продолжить. */
    <div
      ref={ref}
      className="protagonist-intro"
      onClick={onDismiss}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " " || e.key === "Escape") {
          e.preventDefault();
          onDismiss();
        }
      }}
      role="button"
      tabIndex={0}
      aria-label="Закрыть вступление"
    >
      <div className="protagonist-intro__card">
        <div className="protagonist-intro__name">{protagonist.name}</div>
        <div className="protagonist-intro__role">{protagonist.role}</div>
        <div className="protagonist-intro__detail">{protagonist.appearance}</div>
        <div className="protagonist-intro__detail">Характер: {protagonist.traits}</div>
        <div className="protagonist-intro__hint">нажми, чтобы продолжить</div>
      </div>
    </div>
  );
}

function Stage({
  state,
  sessionId,
  scene,
  busy,
  notice,
  onChoose,
  onContinue,
  onRestart,
  onCustomChoice,
  clarify,
  finished,
  settingsOpen,
}) {
  const hasChoices = (scene?.choices ?? []).length > 0;
  const [customText, setCustomText] = useState("");
  const { preferences } = usePreferences();
  const reader = useReader(scene, state, sessionId, preferences.fontSize);
  const { readingRef, page, lastPage } = reader;
  const [panels, setPanels] = useState({ notebook: false, journal: false });
  const togglePanel = name => setPanels(old => {
    const narrow = window.matchMedia('(max-width: 800px)').matches;
    return { ...(narrow ? { notebook: false, journal: false } : old), [name]: !old[name] };
  });
  useEffect(() => {
    const media = window.matchMedia('(max-width: 800px)');
    const changed = () => { if (media.matches) setPanels(old => old.notebook && old.journal ? { notebook: true, journal: false } : old); };
    media.addEventListener('change', changed); return () => media.removeEventListener('change', changed);
  }, []);
  useEffect(() => { setCustomText(''); }, [state?.node_id]);

  // Вступление показываем ровно один раз, на самой первой сцене — не при
  // каждом повторном рендере и не позже по ходу игры. shownIntro живёт в
  // локальном состоянии компонента, не в state с бэкенда — это чисто
  // фронтендовая память "уже показывали в этой игровой сессии".
  const [introDismissed, setIntroDismissed] = useState(false);
  const protagonist = (state?.characters ?? []).find((c) => c.is_protagonist);
  const showIntro = !finished && !introDismissed && (state?.current_scene ?? 0) <= 1 && protagonist;

  // Уведомление о новой информации о персонаже — не про протагониста
  // (у него отдельное вступление по центру), а про любого другого, кто
  // впервые раскрылся (появился в прочитанной сцене). knownIdsRef не
  // триггерит рендер сам по себе — это просто память "кого уже видели",
  // сравнение происходит в эффекте при каждом обновлении state.
  const advanceReading = useCallback(() => {
    if (finished || busy || settingsOpen || showIntro) return;
    if (!reader.advancePage() && !hasChoices) onContinue();
  }, [finished, busy, settingsOpen, showIntro, reader, hasChoices, onContinue]);
  useEffect(() => {
    const keydown = event => {
      if (event.repeat || event.isComposing || event.ctrlKey || event.altKey || event.metaKey || interactiveTarget(event.target)) return;
      if (event.key === 'Enter' || event.key === ' ') {
        if (finished) { event.preventDefault(); return; }
        if (!settingsOpen && !showIntro) { event.preventDefault(); advanceReading(); }
      }
    };
    window.addEventListener('keydown', keydown); return () => window.removeEventListener('keydown', keydown);
  }, [advanceReading, finished, settingsOpen, showIntro]);
  const background = scene?.visual?.backgrounds?.[0];
  const artFailure = (kind, subject, reason) => reportArtFailure(sessionId, state?.node_id, kind, subject, reason);
  const knownIdsRef = useRef(new Set());
  const [revealToast, setRevealToast] = useState(null);
  useEffect(() => {
    const currentlyRevealed = (state?.characters ?? []).filter((c) => c.revealed && !c.is_protagonist);
    const newlyRevealed = currentlyRevealed.filter((c) => !knownIdsRef.current.has(c.id ?? c.name));
    if (newlyRevealed.length > 0) {
      setRevealToast(newlyRevealed[0].name);
      const timer = setTimeout(() => setRevealToast(null), 3500);
      for (const c of currentlyRevealed) knownIdsRef.current.add(c.id ?? c.name);
      return () => clearTimeout(timer);
    }
    for (const c of currentlyRevealed) knownIdsRef.current.add(c.id ?? c.name);
  }, [state?.characters]);

  return (
    <div className={`stage stage--vn ${hasChoices && lastPage && !finished ? "stage--choices" : ""}`}
      data-ui-time={backgroundTime(scene?.visual?.backgrounds?.[0])}
      style={{ '--reading-font-size': `${preferences.fontSize}px` }}
      inert={settingsOpen ? '' : undefined}
      onClick={event => {
        if (interactiveTarget(event.target) || window.getSelection()?.toString()) return;
        advanceReading();
      }}>
      <SceneAmbience effects={scene?.visual?.effects} apiBase={API} active={Boolean(scene) && !finished} />
      <SceneMusic music={scene?.music} apiBase={API} active={Boolean(scene) && !finished} />
      <SceneBackground background={background} effects={scene?.visual?.effects} apiBase={API} onArtFailure={reason => artFailure("background", scene?.location_id || scene?.location || "unknown", reason)} />
      <SceneCharacters characters={scene?.characters ?? []} background={background} effects={scene?.visual?.effects} apiBase={API} activeSpeakerId={!finished && page?.type === 'dialogue' ? page.speaker_id : null} onArtFailure={(id, reason) => artFailure("character", id, reason)} />
      {showIntro && (
        <ProtagonistIntro protagonist={protagonist} onDismiss={() => setIntroDismissed(true)} />
      )}
      {revealToast && (
        <div className="reveal-toast">Появилась информация: {revealToast}</div>
      )}
      <CharacterNotebook state={state} open={panels.notebook} onToggle={() => togglePanel("notebook")} />
      <ChoiceJournal state={state} open={panels.journal} onToggle={() => togglePanel("journal")} />

      <div className="stage__body">
        <div className="stage__upper">
          {!finished && hasChoices && lastPage && !busy && (
            <section className="stage__choices" aria-label="Выбор действия">
              <p className="stage__choice-title">Ваш выбор</p>
              <div className="choices">
                {scene.choices.map((c, i) => (
                  <button key={i} className="choice" onClick={() => onChoose(c)}>
                    <span>{c}</span>
                  </button>
                ))}

                {clarify ? (
                  /* Свой вариант не прошёл проверку — уточняем намерение
                     готовыми вариантами, новый свободный текст здесь уже
                     не принимается. */
                  <div className="custom-choice custom-choice--clarify">
                    <p className="custom-choice__reason">{clarify.reason}</p>
                    <p className="custom-choice__question">{clarify.clarifying_question}</p>
                    {(clarify.clarifying_options ?? []).map((o, i) => (
                      <button key={i} className="choice" onClick={() => onCustomChoice(o, clarify.proposal_id)}>
                        <span>{o}</span>
                      </button>
                    ))}
                  </div>
                ) : state?.build_all_branches === false ? null : (
                  <div className="custom-choice">
                    <input
                      className="field__input"
                      aria-label="Свой вариант действия"
                      value={customText}
                      onChange={(e) => setCustomText(e.target.value)}
                      placeholder="…или напиши свой вариант"
                      maxLength={500}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && customText.trim()) onCustomChoice(customText.trim());
                      }}
                    />
                    <button
                      className="btn btn--ghost"
                      disabled={!customText.trim()}
                      onClick={() => onCustomChoice(customText.trim())}
                    >
                      Своим путём
                    </button>
                  </div>
                )}
              </div>
            </section>
          )}
        </div>
        <section className="dialogue-window" aria-label="Текст сцены">
        <header className="dialogue-window__header">
          {!finished && page?.type === 'dialogue' && <div className="speaker" style={{ color: speakerColor(page.speaker_id) }}>{page.speaker}</div>}
          {!finished && page?.type === 'thought' && <div className="speaker speaker--thought">Мысли</div>}
        </header>
        <div className="dialogue-window__scroll" ref={readingRef} tabIndex={0} style={{ fontSize: `${preferences.fontSize}px` }}>
          {finished ? <div className="finale">
            <h2 className="finale__title">{finaleTitle(scene?.ending)}</h2>
            {reader.blocks.map((block, index) => <div key={index}>
              {block.type === 'dialogue' && <div className="speaker" style={{ color: speakerColor(block.speaker_id) }}>{block.speaker}</div>}
              <p className={`prose prose--${block.type}`}>{block.text}</p>
            </div>)}
          </div> : <>
            {page && <p className={`prose prose--${page.type}`}>{page.text}</p>}
            {notice && <div className="notice">{notice}</div>}
          </>}
        </div>
        <footer className="dialogue-window__footer">
          {finished ? (
            <div className="dialogue-window__ending-actions">
              <button className="btn" onClick={onRestart}>Новая история</button>
              {sessionId && (
                <a className="btn btn--ghost" href={`${API}/story/export/${sessionId}.md`} download>
                  Скачать историю (.md)
                </a>
              )}
            </div>
          ) : (
            <>
            <span className="dialogue-window__location" title={scene?.location}>{scene?.location}</span>
            {busy ? (
              <div className="status ellipsis">Сцена пишется</div>
            ) : hasChoices && lastPage ? (
              <span className="dialogue-window__hint">Выберите действие выше</span>
            ) : (
              // Чистое повествование — история течёт сама, без развилки.
              // Это не запасной вариант на случай ошибки, а нормальный,
              // даже более частый режим сцены (см. правило "выбор — исключение").
              <button className="btn dialogue-window__next" onClick={advanceReading}>
                {!lastPage ? `${reader.index + 1} / ${reader.total} · Далее` : "Продолжить"} <span aria-hidden="true">▸</span>
              </button>
            )}
          </>
        )}
        </footer>
        </section>
      </div>
    </div>
  );
}

function Game() {
  const menuTime = useMenuTime();
  const navigationBusy = useRef(false);
  const starting = useRef(false);
  const [sessionId, setSessionId] = useState(null);
  const [resumable, setResumable] = useState(null); // {session_id, scene, state} | null — сессия с прошлого раза
  const [resumeChecked, setResumeChecked] = useState(false);
  const [pendingResume, setPendingResume] = useState(null); // {session_id, resume_points, genre, tone, target_length} | null — после /story/load_jsonl, до выбора точки
  const [state, setState] = useState(null);
  const [scene, setScene] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [customClarify, setCustomClarify] = useState(null); // {reason, clarifying_question, clarifying_options} | null — свой вариант отклонён
  const [setupError, setSetupError] = useState(null);
  const [finished, setFinished] = useState(false);
  const [backendDown, setBackendDown] = useState(false);

  // Дерево строится в фоне минуты, не секунды — отдельный экран ожидания,
  // не просто busy-спиннер. treeBuilding=true между /story/start и тем
  // моментом, когда опрос /story/tree_status вернул done=true.
  const [treeBuilding, setTreeBuilding] = useState(false);
  const [treeError, setTreeError] = useState(null);
  const [treeNodesBuilt, setTreeNodesBuilt] = useState(0);

  const [settingsStatus, setSettingsStatus] = useState(null); // null = ещё не загружен
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsError, setSettingsError] = useState(null);

  const loadSettingsStatus = useCallback(async () => {
    try {
      const r = await fetch(`${API}/settings`);
      if (!r.ok) return;
      const data = await r.json();
      setSettingsStatus(data);
      // DeepSeek обязателен для генерации текста — без него сразу открываем
      // настройки, а не роняем пользователя в онбординг, который не заработает.
      if (!data.deepseek_api_key) setShowSettings(true);
    } catch {
      /* бэкенд ещё не поднялся — health-check ниже это отдельно обработает */
    } finally {
      setSettingsLoaded(true);
    }
  }, []);

  useEffect(() => {
    fetch(`${API}/health`)
      .then((r) => {
        if (!r.ok) throw new Error();
      })
      .catch(() => setBackendDown(true));
    loadSettingsStatus();

    /* Восстановление после закрытия вкладки. Сессии живут в памяти сервера,
       поэтому сохранённый id может оказаться мёртвым (сервер перезапускали) —
       тогда тихо чистим его и показываем обычный онбординг. */
    const saved = browserStorage.getItem(RESUME_KEY);
    if (!saved) {
      setResumeChecked(true);
      return;
    }
    fetch(`${API}/story/session/${saved}`)
      .then((r) => {
        if (r.status === 404) browserStorage.removeItem(RESUME_KEY);
        return r.ok ? r.json() : Promise.reject();
      })
      .then((data) => setResumable(data))
      .catch(() => {})
      .finally(() => setResumeChecked(true));
  }, [loadSettingsStatus]);

  const clarify = useCallback(async (seed, genre, tone) => {
    setBusy(true);
    setSetupError(null);
    try {
      const r = await fetch(`${API}/story/clarify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seed, genre, tone }),
      });

      if (!r.ok) {
        const err = await r.json();
        setSetupError(
          err.detail?.message ?? "Такую завязку движок не отыграет. Попробуй иначе."
        );
        return null;
      }

      const data = await r.json();
      return data.questions ?? [];
    } catch {
      setSetupError("Бэкенд не отвечает. Проверь, запущен ли server.py.");
      return null;
    } finally {
      setBusy(false);
    }
  }, []);

  const sendCustomChoice = useCallback(
    async (text, proposalId = null) => {
      // busy проверяется и здесь, не только скрытием кнопок: разметка может
      // измениться, а двойная отправка стоит реальных денег — это генерация.
      if (!sessionId || !text || navigationBusy.current) return;
      navigationBusy.current = true;
      setBusy(true);
      setNotice(null);
      try {
        const r = await fetch(`${API}/story/custom_choice`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, custom_text: text, proposal_id: proposalId, from_node_id: state?.node_id }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          if (err.detail?.reason === 'stale_scene') {
            const current = await fetch(`${API}/story/session/${sessionId}`);
            if (current.ok) {
              const data = await current.json();
              setScene(data.scene); setState(data.state); setFinished(data.finished);
              setCustomClarify(null);
            }
          }
          setNotice(apiError(err, "Не удалось обработать свой вариант."));
          return;
        }
        const data = await r.json();
        if (!data.accepted) {
          // Отклонено — показываем уточняющий вопрос вместо сцены.
          setCustomClarify(data);
          return;
        }
        setCustomClarify(null);
        setScene(data.scene);
        setState(data.state);
        setFinished(data.finished);
      } catch {
        setNotice("Бэкенд не отвечает.");
      } finally {
        navigationBusy.current = false;
        setBusy(false);
      }
    },
    [sessionId, state?.node_id]
  );

  const advance = useCallback(
    async (sid, choice) => {
      if (navigationBusy.current) return;
      navigationBusy.current = true;
      setBusy(true);
      setNotice(null);
      // Без таймаута зависший на бэкенде запрос (дедлок пула соединений) оставлял
      // busy=true навсегда: спиннер «Сцена пишется» не снимался, игра
      // вставала. AbortController гарантирует, что через 3 минуты UI выйдет
      // из состояния ожидания и покажет ошибку, даже если ответ не придёт.
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 1200000);
      try {
        const r = await fetch(`${API}/story/scene`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sid, choice, from_node_id: state?.node_id }),
          signal: controller.signal,
        });

        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          const d = err.detail ?? {};
          if (d.reason === 'stale_scene') {
            const current = await fetch(`${API}/story/session/${sid}`);
            if (current.ok) {
              const data = await current.json();
              setScene(data.scene); setState(data.state); setFinished(data.finished);
              setCustomClarify(null);
              setNotice("Позиция синхронизирована с другой вкладкой.");
              return;
            }
          }
          setNotice(
            apiError(err, "Сцену собрать не вышло. Выбери другое направление или начни заново.")
          );
          return;
        }

        const data = await r.json();
        setScene(data.scene);
        setState(data.state);
        setFinished(data.finished);
      } catch (e) {
        // Отмена по таймауту приходит как AbortError и не отличима от
        // сетевого сбоя без этой проверки — сообщение должно быть разным.
        if (e.name === "AbortError") {
          setNotice(
            "Сцена пишется слишком долго. Запрос прерван — попробуй ещё раз. " +
              "Если повторяется, проверь, жив ли server.py."
          );
        } else {
          setNotice("Бэкенд не отвечает. Проверь, запущен ли server.py.");
        }
      } finally {
        clearTimeout(timeoutId);
        navigationBusy.current = false;
        setBusy(false);
      }
    },
    [state?.node_id]
  );

  /* Опрашивает готовность дерева раз в секунду. По завершении — первый
     advance() уходит в уже готовое дерево (см. режим A в /story/scene на
     бэкенде: первый запрос отдаёт корень как есть, реальный текст choice
     здесь не имеет значения, дерево его не читает на этом самом первом шаге). */
  const pollTreeStatus = useCallback(
    async (sid, seed) => {
      // Локальная переменная, не React state — внутри одного длинного
      // async-цикла state, прочитанный из замыкания, был бы устаревшим
      // (setTreeNodesBuilt не обновляет саму переменную немедленно), а для
      // сообщения об ошибке в конце нужно последнее РЕАЛЬНОЕ значение.
      let lastKnownNodes = 0;

      for (let attempt = 0; attempt < 2700; attempt++) {
        let status;
        try {
          const r = await fetch(`${API}/story/tree_status/${sid}`);
          if (!r.ok) {
            setTreeError("Не удалось узнать статус построения истории.");
            setTreeBuilding(false);
            return;
          }
          status = await r.json();
        } catch {
          setTreeError("Бэкенд не отвечает во время построения истории.");
          setTreeBuilding(false);
          return;
        }

        if (typeof status.nodes_built === "number") {
          lastKnownNodes = status.nodes_built;
          setTreeNodesBuilt(status.nodes_built);
        }

        if (status.error) {
          setTreeError(status.error);
          setTreeBuilding(false);
          return;
        }
        if (status.done) {
          setTreeBuilding(false);
          await advance(sid, `Начни историю с завязки: ${seed}`);
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      setTreeError(
        `Построение истории не завершилось за 45 минут (успели построить ${lastKnownNodes} узлов). ` +
        `Похоже, что-то застряло — попробуй начать заново.`
      );
      setTreeBuilding(false);
    },
    [advance]
  );

  const loadJsonlFile = useCallback(async (file) => {
    setBusy(true);
    setSetupError(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const r = await fetch(`${API}/story/load_jsonl`, { method: "POST", body: formData });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setSetupError(err.detail?.message ?? "Не удалось разобрать файл.");
        return;
      }
      const data = await r.json();
      setPendingResume(data);
    } catch {
      setSetupError("Бэкенд не отвечает. Проверь, запущен ли server.py.");
    } finally {
      setBusy(false);
    }
  }, []);

  const resumeAt = useCallback(
    async (nodeId) => {
      if (!pendingResume) return;
      setBusy(true);
      setSetupError(null);
      try {
        const r = await fetch(`${API}/story/resume_at`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: pendingResume.session_id, node_id: nodeId }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          setSetupError(err.detail?.message ?? "Не удалось загрузить эту точку.");
          return;
        }
        const data = await r.json();
        const sid = pendingResume.session_id;
        setSessionId(sid);
        browserStorage.setItem(RESUME_KEY, sid);
        setState(data.state);
        setScene(data.scene);
        setFinished(data.finished);
        setPendingResume(null);
        // Та же причина, что в restart: эти состояния живут в главном
        // компоненте и от смены истории сами не сбрасываются.
        setCustomClarify(null);
        setNotice(null);
      } catch {
        setSetupError("Бэкенд не отвечает.");
      } finally {
        setBusy(false);
      }
    },
    [pendingResume]
  );

  const start = useCallback(
    async (payload) => {
      if (starting.current) return;
      starting.current = true;
      setBusy(true);
      setSetupError(null);
      try {
        const r = await fetch(`${API}/story/start`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        if (!r.ok) {
          const err = await r.json();
          setSetupError(
            err.detail?.message ?? "Не удалось начать историю. Попробуй другую завязку."
          );
          return;
        }

        const data = await r.json();
        setSessionId(data.session_id);
        browserStorage.setItem(RESUME_KEY, data.session_id);
        setState(data.state);

        // Дерево строится в фоне — переключаемся на экран ожидания вместо
        // перехода к первой сцене до готовности начального сегмента.
        setTreeBuilding(true);
        setTreeError(null);
        setTreeNodesBuilt(0);
        await pollTreeStatus(data.session_id, payload.seed);
      } catch {
        setSetupError("Бэкенд не отвечает. Проверь, запущен ли server.py.");
      } finally {
        starting.current = false;
        setBusy(false);
      }
    },
    [pollTreeStatus]
  );

  const restart = () => {
    if (sessionId) {
      fetch(`${API}/story/session/${sessionId}`, { method: "DELETE" }).catch(() => {});
    }
    setSessionId(null);
    browserStorage.removeItem(RESUME_KEY);
    setResumable(null);
    setState(null);
    setScene(null);
    setFinished(false);
    setNotice(null);
    setTreeBuilding(false);
    setTreeError(null);
    setTreeNodesBuilt(0);
    // Уточняющий вопрос от отклонённого своего варианта живёт в главном
    // компоненте и НЕ пересоздаётся вместе со Stage. Без сброса игрок на
    // первой же развилке новой истории увидел бы вопрос из прошлой.
    setCustomClarify(null);
    setSetupError(null);
  };

  /* Восстанавливает историю ровно с того места, где она была сохранена —
     последняя сцена приходит с бэкенда готовой в data.scene (сервер берёт
     её из текущего узла дерева), без
     единого нового вызова генерации. */
  /* Для сцен без развилки (scene.choices пуст) — просто продолжаем историю.
     Единственный смысловой сигнал модели тут — что игрок читал, а не выбирал,
     поэтому текст нейтральный, не описывает несуществующее решение. */
  const continueStory = () => advance(sessionId, "Продолжить");

  const saveSettings = async (payload) => {
    setSettingsSaving(true);
    try {
      const r = await fetch(`${API}/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        const data = await r.json();
        setSettingsStatus(data);
        setSettingsError(null);
        if (data.deepseek_api_key) setShowSettings(false);
      } else {
        // Экран и так останется открытым, но молча — игрок не поймёт, что
        // пошло не так (диск полон, нет прав). Показываем причину.
        const err = await r.json().catch(() => ({}));
        setSettingsError(apiError(err, "Не удалось сохранить настройки."));
      }
    } catch {
      setSettingsError("Бэкенд не отвечает — настройки не сохранены.");
    } finally {
      setSettingsSaving(false);
    }
  };

  if (backendDown) {
    return (
      <MenuFrame>
        <div className="setup__card">
          <p className="eyebrow">Движок историй</p>
          <h1 className="setup__title">Бэкенд не запущен</h1>
          <p className="setup__lede">
            Запусти локальный сервер и обнови окно:
          </p>
          <p className="status">uvicorn server:app --host 127.0.0.1 --port 8756</p>
        </div>
      </MenuFrame>
    );
  }

  if (!settingsLoaded || !resumeChecked) {
    return (
      <MenuFrame>
        <div className="setup__card">
          <p className="status ellipsis">Загрузка</p>
        </div>
      </MenuFrame>
    );
  }

  if (showSettings && !sessionId) {
    return (
      <Settings
        status={settingsStatus ?? {}}
        onSave={saveSettings}
        onSkip={() => setShowSettings(false)}
        saving={settingsSaving}
        canSkip={!!settingsStatus?.deepseek_api_key}
        error={settingsError}
      />
    );
  }

  if (treeBuilding || treeError) {
    return (
      <MenuFrame>
        <div className="setup__card">
          <p className="eyebrow">Движок историй</p>
          <h1 className="setup__title">
            {treeError ? "Не получилось" : <>Готовим<em> начало истории</em></>}
          </h1>
          {treeError ? (
            <>
              <p className="notice">{treeError}</p>
              <button className="btn" onClick={restart}>
                Начать заново
              </button>
            </>
          ) : (
            <>
              <p className="setup__lede">
                Готовим персонажей и события первого сегмента. Текст сцен
                пишется по мере прохождения. Обычный режим готовит события двух
                ближайших продолжений; экономный — только выбранного пути.
                На границах сегментов потребуется подождать.
              </p>
              {treeNodesBuilt > 0 && (
                <p className="setup__lede" style={{ opacity: 0.7 }}>
                  Построено сцен: {treeNodesBuilt}
                </p>
              )}
            </>
          )}
        </div>
      </MenuFrame>
    );
  }

  return (
    <>
      <button
        className="settings-gear"
        data-ui-time={sessionId ? backgroundTime(scene?.visual?.backgrounds?.[0]) : menuTime}
        onClick={() => setShowSettings(true)}
        title="Настройки"
        aria-label="Настройки"
      >
        ⚙
      </button>
      {state?.usage && state.usage.api_calls > 0 && (
        <div className="usage-badge" title="Точный расход токенов из ответов DeepSeek">
          {(state.usage.input_tokens + state.usage.output_tokens).toLocaleString("ru-RU")} ток.
        </div>
      )}
      {showSettings && sessionId && <Settings status={settingsStatus ?? {}} onSave={saveSettings}
        onSkip={() => setShowSettings(false)} saving={settingsSaving} canSkip={true} error={settingsError}
        time={backgroundTime(scene?.visual?.backgrounds?.[0])} overlay />}
      {sessionId && <Spine current={state?.current_scene ?? 0} total={state?.target_length ?? 8} />}
      {sessionId ? (
        <Stage
          key={sessionId}
          state={state}
          settingsOpen={showSettings}
          sessionId={sessionId}
          scene={scene}
          busy={busy}
          notice={notice || state?.save_warning}
          finished={finished}
          onChoose={(c) => {
            setCustomClarify(null);
            advance(sessionId, c);
          }}
          onContinue={continueStory}
          onRestart={restart}
          onCustomChoice={sendCustomChoice}
          clarify={customClarify}
        />
      ) : resumable ? (
        <ResumePrompt
          info={resumable}
          onResume={() => {
            setSessionId(resumable.session_id);
            setState(resumable.state);
            setScene(resumable.scene);
            setFinished(resumable.finished);
            setResumable(null);
            // Здесь состояния и так пусты (это первая загрузка страницы),
            // но полагаться на это не стоит: явный сброс переживёт любую
            // будущую перестановку этого экрана.
            setCustomClarify(null);
            setNotice(null);
          }}
          onDiscard={() => {
            // Убираем и снимок на сервере — иначе брошенная история
            // всплывала бы снова при каждом входе.
            fetch(`${API}/story/session/${resumable.session_id}`, { method: "DELETE" }).catch(() => {});
            browserStorage.removeItem(RESUME_KEY);
            setResumable(null);
          }}
        />
      ) : pendingResume ? (
        <ResumePointsPicker
          info={pendingResume}
          onPick={resumeAt}
          onCancel={() => setPendingResume(null)}
          busy={busy}
          error={setupError}
        />
      ) : (
        <Setup onStart={start} onClarify={clarify} busy={busy} error={setupError} onLoadJsonl={loadJsonlFile} />
      )}
    </>
  );
}

export default function App() {
  return <PreferencesProvider><MenuProvider><Game /></MenuProvider></PreferencesProvider>;
}
