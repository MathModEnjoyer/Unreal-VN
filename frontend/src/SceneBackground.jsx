import { useEffect, useRef, useState } from "react";
import { backingSize, backgroundTime, backgroundPalette, gradePixels, gradeAtmosphere } from "./backgroundProcessing.js";
import SceneEffects from "./SceneEffects.jsx";

// Bound decoded-image memory; no persisted morning/night or enlarged copies.
const images = new Map();
function loadImage(url) {
  if (images.has(url)) {
    const pending = images.get(url);
    images.delete(url);
    images.set(url, pending);
    return pending;
  }
  const pending = new Promise((resolve, reject) => {
    const image = new Image();
    image.crossOrigin = "anonymous";
    image.decoding = "async";
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("Background image unavailable"));
    image.src = url;
  });
  images.set(url, pending);
  while (images.size > 2) images.delete(images.keys().next().value);
  pending.catch(() => {
    if (images.get(url) === pending) images.delete(url);
  });
  return pending;
}

export default function SceneBackground({ background, effects, apiBase = "", onArtFailure }) {
  const report = useRef(onArtFailure); report.current = onArtFailure;
  const container = useRef(null);
  const canvas = useRef(null);
  const draw = useRef(null);
  const appearance = useRef(null);
  const [ready, setReady] = useState(false);
  const path = background?.url;
  // Only serve the local daytime pack, including when opening an old save.
  const url = /^\/backgrounds\/[a-z_]+__day\.(webp|png)$/.test(path || "") ? `${apiBase}${path}` : null;
  const time = backgroundTime(background);
  const profile = background?.lighting_profile || "outdoor";
  const palette = JSON.stringify(backgroundPalette(background, effects));

  useEffect(() => {
    let cancelled = false;
    let frame = null;
    let finish = null;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const identity = JSON.stringify([url, profile]);
    const samePlace = appearance.current?.identity === identity;
    if (!samePlace) {
      setReady(false);
      appearance.current = null;
      draw.current = null;
    }
    if (url) loadImage(url).then((image) => {
      if (cancelled) return;
      const source = document.createElement("canvas");
      source.width = image.naturalWidth;
      source.height = image.naturalHeight;
      const context = source.getContext("2d", { willReadFrequently: true });
      if (!context) throw new Error("Canvas unavailable");
      context.drawImage(image, 0, 0);
      if (time !== "day" || palette !== "null") {
        const frame = context.getImageData(0, 0, source.width, source.height);
        gradePixels(frame.data, time, profile);
        gradeAtmosphere(frame.data, JSON.parse(palette), time, profile);
        context.putImageData(frame, 0, 0);
      }
      // Snapshot the last displayed native-size frame when a transition is
      // interrupted. Every new target still starts from the original artwork.
      const from = samePlace ? appearance.current?.surface : null;
      const surface = document.createElement("canvas");
      surface.width = source.width; surface.height = source.height;
      const blend = surface.getContext("2d");
      if (!blend) throw new Error("Canvas unavailable");
      const started = performance.now();
      const animate = from && !media.matches && !document.hidden;
      let complete = !animate;
      appearance.current = { identity, surface };
      const render = (now = performance.now()) => {
        if (cancelled || !canvas.current || !container.current) return;
        const progress = complete ? 1 : Math.min(1, (now - started) / 2400);
        const mix = progress * progress * (3 - 2 * progress);
        blend.globalAlpha = 1;
        blend.clearRect(0, 0, surface.width, surface.height);
        if (from && mix < 1) blend.drawImage(from, 0, 0);
        blend.globalAlpha = mix;
        blend.drawImage(source, 0, 0);
        blend.globalAlpha = 1;
        const bounds = container.current.getBoundingClientRect();
        const size = backingSize(source.width, source.height, bounds.width, bounds.height, window.devicePixelRatio);
        const output = canvas.current;
        if (output.width !== size.width || output.height !== size.height) {
          output.width = size.width;
          output.height = size.height;
        }
        const target = output.getContext("2d");
        if (!target) throw new Error("Canvas unavailable");
        target.imageSmoothingEnabled = true;
        target.imageSmoothingQuality = "high";
        target.clearRect(0, 0, size.width, size.height);
        target.drawImage(surface, 0, 0, size.width, size.height);
        container.current.dataset.paletteTransition = String(mix < 1);
        setReady(true);
        if (progress === 1) complete = true;
      };
      const tick = (now) => {
        frame = null;
        render(now);
        if (!complete && !cancelled) frame = requestAnimationFrame(tick);
      };
      finish = () => {
        if (media.matches || document.hidden) {
          if (frame !== null) cancelAnimationFrame(frame);
          frame = null; complete = true; render();
        }
      };
      draw.current = () => render();
      tick(started);
    }).catch(() => {
      // Missing/corrupt assets leave the neutral backdrop; reading stays available.
      if (!cancelled) { draw.current = null; setReady(false); report.current?.("image_load_failed"); }
    });
    const settle = () => finish?.();
    media.addEventListener("change", settle);
    document.addEventListener("visibilitychange", settle);
    return () => {
      cancelled = true;
      draw.current = null;
      if (frame !== null) cancelAnimationFrame(frame);
      media.removeEventListener("change", settle);
      document.removeEventListener("visibilitychange", settle);
    };
  }, [url, time, profile, palette]);

  useEffect(() => {
    const resize = () => draw.current?.();
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(resize);
      observer.observe(container.current);
      return () => observer.disconnect();
    }
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);

  return (
    <div ref={container} className="stage__backdrop" aria-hidden="true" data-background-ready={ready} data-time-of-day={time}>
      <canvas ref={canvas} className="stage__background-image" style={{ opacity: ready ? 1 : 0 }} />
      <SceneEffects effects={effects} active={ready && appearance.current?.identity === JSON.stringify([url, profile])} />
    </div>
  );
}
