// A single procedural gouache sphere; no model or extra emotion PNGs.
export function paintPlaceholder() {
  const canvas = document.createElement('canvas'); canvas.width = 512; canvas.height = 768;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('Canvas unavailable');
  const image = ctx.createImageData(512, 768);
  let seed = 7031;
  const random = () => { seed = (1664525 * seed + 1013904223) >>> 0; return seed / 4294967296; };
  for (let y = 0; y < 768; y++) for (let x = 0; x < 512; x++) {
    const dx = (x - 256) / 187, dy = (y - 330) / 187;
    const r = Math.hypot(dx, dy), i = (y * 512 + x) * 4;
    const noise = (random() - .5) * 12;
    if (r < 1) {
      const z = Math.sqrt(1 - r * r), light = Math.max(0, -.4 * dx - .5 * dy + .75 * z);
      const brush = 4 * Math.sin(x * .11 + Math.sin(y * .07) * 3) + noise;
      image.data[i] = 87 + 109 * light + brush;
      image.data[i + 1] = 100 + 109 * light + brush;
      image.data[i + 2] = 119 + 99 * light + brush;
      image.data[i + 3] = Math.min(255, (1 - r) * 8000);
    }
  }
  ctx.putImageData(image, 0, 0);
  return canvas;
}
