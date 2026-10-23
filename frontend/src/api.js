export async function apiFetch(url, options = {}) {
  const controller = new AbortController();
  // Generation can include several bounded server retries. A short browser
  // timeout would abandon a paid response while the server was still working.
  const generation = options.method === 'POST' && /\/story\/(scene|custom_choice|start|resume_at|clarify|genre_tone_fit)$/.test(url);
  const premises = (!options.method || options.method === 'GET') && url === '/story/premises';
  const timer = setTimeout(() => controller.abort(), generation ? 1_200_000 : premises ? 240_000 : 30_000);
  const abort = () => controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener('abort', abort);
  }
}

export function apiError(data, fallback) {
  const detail = data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  return typeof detail?.message === 'string' ? detail.message : fallback;
}
