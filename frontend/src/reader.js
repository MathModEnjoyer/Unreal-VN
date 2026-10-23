// Text presentation only: no generation, story mutations or new API calls.
export function sceneBlocks(scene, cast = []) {
  if (Array.isArray(scene?.blocks) && scene.blocks.length) {
    return scene.blocks.filter(b => ['narration', 'dialogue', 'thought'].includes(b.type) && typeof b.text === 'string')
      .map(b => ({ ...b, speaker: cast.find(c => c.id === b.speaker_id)?.name || b.speaker || 'Неизвестный собеседник' }));
  }
  const blocks = [];
  if (scene?.narration) blocks.push({ type: 'narration', text: scene.narration });
  if (scene?.dialogue && !scene.narration?.includes(scene.dialogue))
    blocks.push({ type: 'dialogue', text: scene.dialogue, speaker_id: scene.speaker_id, speaker: scene.speaker });
  return blocks;
}

export function speakerColor(id) {
  let hash = 0;
  for (const char of String(id || 'unknown')) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  // Bright, distinct ink on every dark time-of-day panel.
  return ['#ffdbab', '#b8e6ff', '#e8c5ff', '#b9f2cf', '#ffc2d1', '#f3eda7', '#c9d4ff', '#b6eee9'][hash % 8];
}

export function paginateBlocks(blocks, width, maxLines, measure) {
  const pages = [];
  width = Math.max(30, width); maxLines = Math.max(1, maxLines);
  blocks.forEach((block, blockIndex) => {
    const text = block.text;
    let start = 0;
    while (start < text.length) {
      while (start < text.length && /\s/u.test(text[start])) start++;
      if (start >= text.length) break;
      const blank = /\n\s*\n/gu; blank.lastIndex = start;
      const paragraphEnd = blank.exec(text)?.index ?? text.length;
      let end = start, line = '', lines = 1;
      // Iterate words with original offsets: resizing never loses the anchor.
      const words = /\S+\s*/gu; words.lastIndex = start;
      let match;
      while ((match = words.exec(text)) && match.index < paragraphEnd) {
        const raw = match[0].slice(0, paragraphEnd - match.index);
        const word = raw.trimEnd();
        const newLines = (raw.match(/\n/gu) || []).length;
        if (measure(line + word) > width) {
          if (line) { lines++; line = ''; }
          if (lines > maxLines) break;
          if (measure(word) > width) {
            let consumed = 0;
            for (const char of word) {
              if (measure(line + char) > width && line) { lines++; line = ''; }
              if (lines > maxLines) break;
              line += char; consumed += char.length;
            }
            end = match.index + consumed;
            if (consumed < word.length) break;
            line += ' ';
          } else { line = word + ' '; end = match.index + raw.length; }
        } else { line += word + ' '; end = match.index + raw.length; }
        if (newLines) { lines += newLines; line = ''; }
        if (lines >= maxLines && newLines) break;
      }
      if (end <= start) end = Math.min(start + 1, text.length);
      pages.push({ ...block, blockIndex, start, end, text: text.slice(start, end).trim() });
      start = end;
    }
  });
  return pages;
}

export function pageAt(pages, anchor) {
  let index = pages.findIndex(p => p.blockIndex === anchor.block && p.end > anchor.offset);
  if (index < 0) index = pages.findIndex(p => p.blockIndex > anchor.block);
  return index < 0 ? Math.max(0, pages.length - 1) : index;
}

export function interactiveTarget(target) {
  return Boolean(target?.closest?.('button,a,input,textarea,select,label,[role="button"],[contenteditable="true"],.notebook,.journal,.stage__choices,.settings-overlay'));
}
