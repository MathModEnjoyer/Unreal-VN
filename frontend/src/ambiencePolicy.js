// Input is the server-approved effect, never a keyword in narration/dialogue.
export function ambienceTargets(effects, enabled, active = true) {
  const type = effects?.weather?.type;
  const intensity = Number(effects?.weather?.intensity);
  const amount = Number.isFinite(intensity) ? Math.max(0, Math.min(1, intensity)) : 0.5;
  const volume = enabled && active ? 0.08 + amount * 0.24 : 0;
  return { rain: type === "rain" ? volume : 0, storm: type === "storm" ? volume : 0 };
}
