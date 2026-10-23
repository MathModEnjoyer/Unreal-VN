// One daytime source; these transforms change colors, never pixel positions.
const NIGHT_PROFILES = {
  outdoor: { white: [83, 105, 146], mix: 0.72 },
  alien: { white: [83, 105, 146], mix: 0.72 },
  windowed: { white: [111, 120, 152], mix: 0.60 },
  enclosed: { white: [126, 130, 148], mix: 0.48 },
  space: { white: [105, 120, 146], mix: 0.62 },
};
const NIGHT_BLACK = [4, 9, 25];
const NIGHT_GAINS = [0.30, 0.36, 0.52];
const CURVE = Array.from({ length: 256 }, (_, value) => Math.round(255 * (value / 255) ** 1.15));
const clampByte = (value) => Math.max(0, Math.min(255, Math.round(value)));

// These are approved server descriptors, never guesses from story text.
export function backgroundPalette(background, effects) {
  if (backgroundTime(background) === "neutral" ||
      !["open", "sheltered"].includes(background?.effect_profile?.setting)) return null;
  return effects?.grading || null;
}

const PALETTES = {
  winter: { saturation: .78, gain: [.96, .99, 1.03], lift: [1, 2, 3] },
  rain: { saturation: .72, gain: [.81, .87, .94], lift: [0, 1, 3] },
  storm: { saturation: .63, gain: [.64, .73, .84], lift: [0, 1, 3] },
  snow: { saturation: .67, gain: [.95, .98, 1.01], lift: [8, 9, 10] },
  fog: { saturation: .55, gain: [.84, .86, .87], lift: [23, 24, 23] },
  ash: { saturation: .57, gain: [.89, .85, .80], lift: [3, 2, 1] },
};

export function gradeAtmosphere(pixels, palette, timeOfDay = "day", lightingProfile = "outdoor") {
  if (!palette || timeOfDay === "neutral" || ["fixed", "enclosed", "space", "alien"].includes(lightingProfile)) return pixels;
  const strength = Number.isFinite(palette.strength) ? Math.max(0, Math.min(1, palette.strength)) : 0;
  const layers = [];
  if (palette.season === "winter") layers.push([PALETTES.winter, .6]);
  for (const field of ["weather", "ambient"]) {
    const layer = palette[field];
    if (layer && PALETTES[layer.type] && Number.isFinite(layer.intensity)) {
      layers.push([PALETTES[layer.type], Math.max(0, Math.min(1, layer.intensity))]);
    }
  }
  const night = timeOfDay === "night";
  for (const [style, amount] of layers) {
    // Night already has a strong grade: preserve legibility and dark fog.
    const mix = strength * amount * (night ? .5 : 1);
    if (!mix) continue;
    for (let index = 0; index < pixels.length; index += 4) {
      const gray = .299 * pixels[index] + .587 * pixels[index + 1] + .114 * pixels[index + 2];
      for (let channel = 0; channel < 3; channel++) {
        const value = pixels[index + channel];
        const graded = (gray + (value - gray) * style.saturation) * style.gain[channel] + style.lift[channel] * (night ? .18 : 1);
        pixels[index + channel] = clampByte(value + (graded - value) * mix);
      }
    }
  }
  return pixels;
}

export function backgroundTime(background) {
  if (background?.is_fallback || background?.time_mode === "fixed") return "neutral";
  if (!background?.time_mode && background?.asset_id === "metro_car") return "neutral";
  // Old saves have no explicit mode; their enclosed/space profiles stay static.
  if (!background?.time_mode && ["enclosed", "space", "fixed"].includes(background?.lighting_profile)) return "neutral";
  return background?.time_of_day || "day";
}

export function gradePixels(pixels, timeOfDay = "day", lightingProfile = "outdoor") {
  if (["fixed", "enclosed", "space"].includes(lightingProfile)) return pixels;
  if (timeOfDay === "day" || !["morning", "evening", "night"].includes(timeOfDay)) return pixels;
  const profile = NIGHT_PROFILES[lightingProfile] || NIGHT_PROFILES.outdoor;
  const gains = timeOfDay === "evening" ? [0.90, 0.68, 0.61] : [1.04, 0.93, 0.77];
  const offsets = timeOfDay === "evening" ? [12, 5, 9] : [10, 6, 8];
  for (let index = 0; index < pixels.length; index += 4) {
    if (timeOfDay === "night") {
      const gray = CURVE[clampByte(0.299 * pixels[index] + 0.587 * pixels[index + 1] + 0.114 * pixels[index + 2])];
      for (let channel = 0; channel < 3; channel += 1) {
        const tonal = NIGHT_BLACK[channel] + (profile.white[channel] - NIGHT_BLACK[channel]) * gray / 255;
        pixels[index + channel] = clampByte(pixels[index + channel] * NIGHT_GAINS[channel] * (1 - profile.mix) + tonal * profile.mix);
      }
    } else {
      for (let channel = 0; channel < 3; channel += 1) {
        pixels[index + channel] = clampByte(pixels[index + channel] * gains[channel] + offsets[channel]);
      }
    }
  }
  return pixels;
}

export function backingSize(sourceWidth, sourceHeight, frameWidth, frameHeight, pixelRatio = 1) {
  const ratio = Math.max(1, Math.min(2, pixelRatio || 1));
  const scale = Math.max(frameWidth / sourceWidth, frameHeight / sourceHeight) * ratio;
  const width = Math.max(1, Math.min(1920, Math.ceil(sourceWidth * scale)));
  return { width, height: Math.max(1, Math.round(width * sourceHeight / sourceWidth)) };
}
