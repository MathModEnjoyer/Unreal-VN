import { useCallback, useEffect, useState } from 'react';
import { loadPremises } from './premiseSuggestions.js';

export function usePremiseSuggestions() {
  const [premises, setPremises] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt(value => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadPremises(controller.signal)
      .then(data => { if (!controller.signal.aborted) setPremises(data); })
      .catch(reason => {
        if (!controller.signal.aborted) {
          setError(reason.name === 'AbortError'
            ? 'DeepSeek отвечает слишком долго. Можно повторить загрузку.'
            : reason.message || 'Не удалось загрузить завязки. Проверь подключение и попробуй ещё раз.');
        }
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [attempt]);

  return { premises, loading, error, retry };
}

export default function PremiseSuggestions({ seed, onPick, suggestions }) {
  const { premises, loading, error, retry } = suggestions;
  return (
    <div className="field" aria-busy={loading}>
      <span className="field__label">
        {loading ? 'Придумываем варианты…' : 'Готовая завязка — или своя ниже'}
      </span>
      {error && (
        <div className="notice" role="alert">
          <p>{error}</p>
          <button className="btn btn--ghost" onClick={retry} disabled={loading}>
            Повторить загрузку завязок
          </button>
        </div>
      )}
      <div className="chips">
        {loading ? [0, 1, 2].map(i => <span key={i} className="chip chip--skeleton">…</span>)
          : premises.map((p, i) => (
            <button key={i} className={`chip ${seed === p.text ? 'chip--on' : ''}`} onClick={() => onPick(p.text)}>
              {p.genre || `Вариант ${i + 1}`}
            </button>
          ))}
      </div>
    </div>
  );
}
