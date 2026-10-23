import { apiFetch, apiError } from './api.js';

export async function loadPremises(signal) {
  const response = await apiFetch('/story/premises', { signal });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(apiError(data, 'Не удалось получить завязки. Попробуй ещё раз.'));
  }
  const premises = Array.isArray(data?.premises)
    ? data.premises.filter(p => p && typeof p.text === 'string' && p.text.trim())
    : [];
  if (!premises.length) throw new Error('Модель не вернула завязки. Попробуй ещё раз.');
  return premises;
}
