// The browser's wall clock follows the player's device, including Termux clients.
export function localMenuTime(date = new Date()) {
  const hour = date.getHours();
  if (hour >= 5 && hour < 11) return 'morning';
  if (hour >= 11 && hour < 17) return 'day';
  if (hour >= 17 && hour < 22) return 'evening';
  return 'night';
}

const FALLBACK = {
  asset_id: 'forest_path', url: '/backgrounds/forest_path__day.webp',
  time_mode: 'outdoor', lighting_profile: 'outdoor',
};

export function chooseMenuBackground(candidates, random = Math.random) {
  const eligible = Array.isArray(candidates) ? candidates.filter(item =>
    item && !item.is_fallback && ['outdoor', 'windowed'].includes(item.time_mode) &&
    item.lighting_profile === item.time_mode &&
    /^\/backgrounds\/[a-z_]+__day\.(webp|png)$/.test(item.url || '')
  ) : [];
  return eligible.length ? eligible[Math.min(eligible.length - 1, Math.floor(random() * eligible.length))] : FALLBACK;
}
