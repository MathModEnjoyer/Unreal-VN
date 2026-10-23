export const CHARACTER_FADE_MS = 450;

// Departing figures retain their last slot and never squeeze the new cast.
export function characterSlots(characters, previous = [], space = {width: 1000, height: 500, fullHeight: 900}) {
  const ids = new Set(characters.map(character => character.id));
  const crowd = characters.length > 3;
  const capacity = Math.max(1, Math.floor(space.width / (space.width < 700 ? 120 : 200)));
  const rows = crowd ? Math.ceil(characters.length / capacity) : 1;
  const columns = characters.length ? Math.ceil(characters.length / rows) : 1;
  const width = 100 / columns;
  const height = crowd ? space.height / rows : space.fullHeight;
  return [
    ...characters.map((character, index) => {
      const row = Math.floor(index / columns);
      const rowCount = Math.min(columns, characters.length - row * columns);
      const center = (100 - rowCount * width) / 2 + (index % columns + .5) * width;
      return {character, leaving: false, center, width, top: (space.top || 0) + row * height, height, crowd,
        portraitHeight: Math.min(height * 2.7, Math.max(1, space.width / columns - 16) * 5)};
    }),
    ...previous.filter(item => !ids.has(item.character.id)).map(item => ({ ...item, leaving: true })),
  ];
}
