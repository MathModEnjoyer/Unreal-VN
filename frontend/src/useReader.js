import { useLayoutEffect, useMemo, useRef, useState, useEffect } from 'react';
import { sceneBlocks, paginateBlocks, pageAt } from './reader.js';
import { browserStorage } from './browserStorage.js';

export function useReader(scene, state, sessionId, fontSize) {
  const readingRef = useRef(null);
  const blocks = useMemo(() => sceneBlocks(scene, state?.characters), [scene, state?.characters]);
  const identity = `${sessionId}:${state?.node_id || state?.current_scene || ''}`;
  const saved = () => {
    try {
      const value = JSON.parse(browserStorage.getItem(`story_reader:${identity}`));
      if (Number.isInteger(value?.block) && value.block >= 0 && Number.isInteger(value?.offset) && value.offset >= 0) return value;
    } catch { /* optional */ }
    return { block: 0, offset: 0 };
  };
  const [position, setPosition] = useState(() => ({ identity, anchor: saved() }));
  const anchor = position.identity === identity ? position.anchor : saved();
  const [layout, setLayout] = useState({ width: 600, lines: 3, font: `${fontSize}px Georgia` });
  useLayoutEffect(() => {
    const node = readingRef.current;
    if (!node) return;
    const measure = () => {
      const style = getComputedStyle(node);
      const next = { width: Math.max(30, node.clientWidth - parseFloat(style.paddingRight || 0) - 8),
        lines: Math.max(1, Math.floor((node.clientHeight - 8) / (fontSize * 1.5))),
        font: `${fontSize}px ${style.fontFamily}` };
      setLayout(old => JSON.stringify(old) === JSON.stringify(next) ? old : next);
    };
    measure();
    const observer = new ResizeObserver(measure); observer.observe(node);
    let alive = true;
    document.fonts?.ready.then(() => { if (alive) measure(); });
    return () => { alive = false; observer.disconnect(); };
  }, [fontSize, identity]);
  const pages = useMemo(() => {
    const context = document.createElement('canvas').getContext('2d');
    if (context) context.font = layout.font;
    return paginateBlocks(blocks, layout.width, layout.lines,
      text => (context?.measureText(text).width ?? text.length * fontSize * .65) + text.length * fontSize * .01);
  }, [blocks, layout, fontSize]);
  const index = pageAt(pages, anchor);
  useEffect(() => { if (readingRef.current) readingRef.current.scrollTop = 0; }, [identity, index]);
  function advancePage() {
    const next = pages[index + 1];
    if (!next) return false;
    const nextAnchor = { block: next.blockIndex, offset: next.start };
    setPosition({ identity, anchor: nextAnchor });
    browserStorage.setItem(`story_reader:${identity}`, JSON.stringify(nextAnchor));
    return true;
  }
  return { readingRef, blocks, page: pages[index], index, total: pages.length, lastPage: index >= pages.length - 1, advancePage };
}
