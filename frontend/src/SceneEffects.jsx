import { useEffect, useRef, useState } from "react";

// Polygons use original-image coordinates; object-fit: cover may crop them.
function imagePoint(point, width, height) {
  const scale = Math.max(width / 16, height / 9);
  return [(width - 16 * scale) / 2 + point[0] * 16 * scale,
    (height - 9 * scale) / 2 + point[1] * 9 * scale];
}
function polygonPath(context, polygon, width, height) {
  polygon.forEach((point, i) => {
    const [x, y] = imagePoint(point, width, height);
    if (i === 0) context.moveTo(x, y); else context.lineTo(x, y);
  });
  context.closePath();
}
function clipRegions(context, polygons, width, height) {
  context.beginPath();
  for (const polygon of polygons) polygonPath(context, polygon, width, height);
  context.clip();
}
function inside(point, polygon) {
  let hit = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[i], b = polygon[j];
    if ((a[1] > point[1]) !== (b[1] > point[1]) &&
        point[0] < (b[0] - a[0]) * (point[1] - a[1]) / (b[1] - a[1]) + a[0]) hit = !hit;
  }
  return hit;
}
function regionPoint(polygon) {
  if (!polygon) return [Math.random(), Math.random()];
  const xs = polygon.map(p => p[0]), ys = polygon.map(p => p[1]);
  const left = Math.min(...xs), top = Math.min(...ys);
  const width = Math.max(...xs) - left, height = Math.max(...ys) - top;
  for (let i = 0; i < 100; i++) {
    const point = [left + Math.random() * width, top + Math.random() * height];
    if (inside(point, polygon)) return point;
  }
  return polygon[0];
}

export default function SceneEffects({ effects, active }) {
  const canvas = useRef(null);
  const played = useRef(new Set());
  const weatherFade = useRef(null);
  const [reduced, setReduced] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const weather = effects?.weather || null;
  const ambient = effects?.ambient || null;
  const signature = JSON.stringify([weather, ambient, effects?.events || [], effects?.scene_key || ""]);

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    const element = canvas.current;
    const context = element?.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, element.width, element.height);
    if (!active || reduced) { weatherFade.current = null; return; }
    const [weather, ambient, events, sceneKey] = JSON.parse(signature);
    const weatherType = weather?.type;
    const ambientType = ambient?.type;
    const strength = Math.max(0, Math.min(1, weather?.intensity || 0));
    const ambientStrength = Math.max(0, Math.min(1, ambient?.intensity || 0));
    const hasWeather = strength > 0 && ["rain", "storm", "snow", "fog"].includes(weatherType);
    const weatherKey = JSON.stringify(weather);
    if (!hasWeather) weatherFade.current = null;
    else if (weatherFade.current?.key !== weatherKey) weatherFade.current = {key: weatherKey, started: performance.now()};
    const hasAmbient = ambientStrength > 0 && ["fireflies", "ash", "embers", "sparks", "dust_motes"].includes(ambientType);
    const event = events.find(event => event.type === "flash");
    const freshEvent = event && sceneKey && !played.current.has(sceneKey);
    if (!hasWeather && !hasAmbient && !freshEvent) return;
    let flash = freshEvent ? {start: performance.now(), event} : null;
    let frame = null, width = 1, height = 1, ratio = 1, last = performance.now();
    const started = weatherFade.current?.started ?? last;
    let nextLightning = last + 10000 + Math.random() * 8000;
    const makeParticle = region => ({point: regionPoint(region), speed: .4 + Math.random(), phase: Math.random() * Math.PI * 2});
    const weatherCount = Math.ceil((weatherType === "snow" ? 65 : weatherType === "fog" ? 8 : 130) * strength);
    const ambientCount = Math.ceil((ambientType === "fireflies" ? 24 : 45) * ambientStrength);
    const drops = hasWeather ? Array.from({length: weatherCount}, () => makeParticle(null)) : [];
    const motes = hasAmbient ? Array.from({length: ambientCount}, () => makeParticle(ambient.region)) : [];

    function resize() {
      const bounds = element.getBoundingClientRect();
      width = Math.max(1, bounds.width); height = Math.max(1, bounds.height);
      ratio = Math.min(window.devicePixelRatio || 1, 1.25, 1600 / width, 1000 / height);
      element.width = Math.round(width * ratio); element.height = Math.round(height * ratio);
    }
    function draw(now) {
      frame = null;
      if (document.hidden) return;
      const delta = Math.min(.05, Math.max(0, (now - last) / 1000));
      last = now;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      if (hasWeather) {
        context.save();
        context.globalAlpha = Math.min(1, (now - started) / 2400);
        if (weather.mode === "masked") clipRegions(context, weather.masks || [], width, height);
        for (const particle of drops) {
          const point = particle.point;
          if (weatherType === "fog") {
            point[0] = (point[0] + delta * .005 * particle.speed) % 1;
            context.fillStyle = `rgba(219,225,221,${.045 * strength})`;
            context.beginPath();
            context.ellipse(point[0] * width, point[1] * height, width * .4, height * .15, 0, 0, Math.PI * 2);
            context.fill();
          } else if (weatherType === "snow") {
            point[0] += Math.sin(now * .0005 + particle.phase) * delta * .025;
            point[1] += delta * .055 * particle.speed;
            if (point[1] > 1.02) { point[0] = Math.random(); point[1] = -.02; }
            context.fillStyle = "rgba(246,249,250,.8)";
            context.beginPath(); context.arc(point[0] * width, point[1] * height, 1 + particle.speed, 0, Math.PI * 2); context.fill();
          } else {
            point[0] += delta * .15 * particle.speed; point[1] += delta * .95 * particle.speed;
            if (point[1] > 1.1 || point[0] > 1.1) { point[0] = Math.random() * 1.1 - .1; point[1] = -.1; }
            context.strokeStyle = `rgba(205,223,235,${.22 + strength * .27})`;
            context.lineWidth = particle.speed > 1 ? 1.2 : .7;
            context.beginPath(); context.moveTo(point[0] * width, point[1] * height);
            context.lineTo(point[0] * width + 6, point[1] * height + 24 * particle.speed); context.stroke();
          }
        }
        context.restore();
        if (weatherType === "storm" && now >= nextLightning && !flash) {
          flash = {start: now, event: {intensity: .24, mode: weather.mode, masks: weather.masks}};
          nextLightning = now + 12000 + Math.random() * 12000;
        }
      }
      if (hasAmbient) {
        context.save();
        if (ambient.region) clipRegions(context, [ambient.region], width, height);
        for (const particle of motes) {
          const point = particle.point;
          point[0] += Math.sin(now * .0004 + particle.phase) * delta * .01;
          point[1] += delta * (ambientType === "ash" ? .025 : ambientType === "sparks" ? -.12 : -.008) * particle.speed;
          if (point[1] < 0 || point[1] > 1 || (ambient.region && !inside(point, ambient.region))) particle.point = regionPoint(ambient.region);
          const [x, y] = imagePoint(particle.point, width, height);
          const glow = .4 + .6 * Math.sin(now * .001 + particle.phase) ** 2;
          const colors = {fireflies: `rgba(232,244,143,${glow})`, ash: "rgba(183,180,172,.6)",
            embers: `rgba(255,159,72,${glow})`, sparks: "rgba(255,218,139,.9)", dust_motes: "rgba(239,226,196,.25)"};
          if (ambientType === "fireflies" || ambientType === "embers") {
            const gradient = context.createRadialGradient(x, y, 0, x, y, 7);
            gradient.addColorStop(0, colors[ambientType]); gradient.addColorStop(1, "transparent");
            context.fillStyle = gradient; context.fillRect(x - 7, y - 7, 14, 14);
          } else {
            context.fillStyle = colors[ambientType];
            context.beginPath(); context.arc(x, y, 1.2 * particle.speed, 0, Math.PI * 2); context.fill();
          }
        }
        context.restore();
      }
      if (flash) {
        const age = now - flash.start;
        if (age > 350) flash = null;
        else {
          if (freshEvent && !played.current.has(sceneKey)) {
            played.current.add(sceneKey);
            if (played.current.size > 1000) played.current.delete(played.current.values().next().value);
          }
          context.save();
          if (flash.event.mode === "masked") clipRegions(context, flash.event.masks || [], width, height);
          context.fillStyle = `rgba(255,249,230,${Math.min(.55, flash.event.intensity ?? .3) * (1 - age / 350)})`;
          context.fillRect(0, 0, width, height); context.restore();
        }
      }
      if (hasWeather || hasAmbient || flash) frame = requestAnimationFrame(draw);
    }
    function visibility() {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      if (!document.hidden) {
        last = performance.now();
        nextLightning = Math.max(nextLightning, last + 8000);
        frame = requestAnimationFrame(draw);
      }
    }
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    document.addEventListener("visibilitychange", visibility);
    visibility();
    return () => {
      if (frame !== null) cancelAnimationFrame(frame);
      observer.disconnect(); document.removeEventListener("visibilitychange", visibility);
      context.setTransform(1, 0, 0, 1, 0, 0); context.clearRect(0, 0, element.width, element.height);
    };
  }, [signature, active, reduced]);

  return <canvas ref={canvas} className="scene-effects" aria-hidden="true"
    data-weather={active && !reduced ? weather?.type || "none" : "none"}
    data-ambient={active && !reduced ? ambient?.type || "none" : "none"} />;
}
