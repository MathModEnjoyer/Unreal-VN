import { useEffect, useState } from 'react';
import { apiFetch } from './api.js';

const reasons = { no_selection: 'Фон не указан', model_fallback: 'Модель не нашла подходящего фона',
  invalid_background_id: 'Неизвестный ID фона', asset_unavailable: 'Файл отсутствует или повреждён',
  catalog_unavailable: 'Каталог недоступен', unknown_gender: 'Пол не указан',
  incompatible_gender: 'Спрайт не соответствует полу', no_compatible_sprite: 'Нет совместимого спрайта',
  image_load_failed: 'Изображение не загрузилось', canvas_failed: 'Ошибка обработки изображения' };
export function reportArtFailure(sessionId, nodeId, kind, subject, reason) {
  if (!sessionId || !nodeId) return;
  apiFetch('/visual/fallbacks', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, node_id: nodeId, kind, subject, reason }) }).catch(() => {});
}
export default function FallbackLog() {
  const [report, setReport] = useState(null), [error, setError] = useState(''), [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    apiFetch('/visual/fallbacks', { signal: controller.signal }).then(async r => {
      if (!r.ok) throw new Error();
      const data = await r.json(); if (!controller.signal.aborted) setReport(data);
    }).catch(() => { if (!controller.signal.aborted) setError('Не удалось прочитать журнал заглушек.'); });
    return () => controller.abort();
  }, [reload]);
  return <section className="fallback-log" aria-label="Логирование заглушек">
    <h2>Логирование заглушек</h2>
    {error && <p role="alert">{error}</p>}
    {!report && !error && <p>Загрузка журнала…</p>}
    {report && <><p>Записей по сценам: {report.scene_count}</p>
      {!report.groups.length && <p>Использование заглушек пока не зарегистрировано.</p>}
      <div className="fallback-log__list">{report.groups.map((g, i) => <details key={i}>
        <summary>{g.kind === 'character' ? 'Персонаж' : 'Фон'}: {g.context?.name || g.context?.location || g.subject} · {g.count}</summary>
        <p>{g.category === 'technical' ? 'Технический сбой' : 'Подбор контента'}: {reasons[g.reason] || g.reason}</p>
        {g.context?.appearance && <p>Внешность: {g.context.appearance}</p>}
        <p>{g.context?.narration}</p>
      </details>)}</div>
      <a className="btn btn--ghost" href="/visual/fallbacks/export" download>Скачать журнал (.json)</a></>}
    <button className="btn btn--ghost" onClick={() => setReload(v => v + 1)}>Обновить журнал</button>
  </section>;
}
