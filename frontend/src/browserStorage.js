// Storage is optional: private browsing and quota errors must not stop a story.
export const browserStorage = {
  getItem(key) { try { return window.localStorage.getItem(key); } catch { return null; } },
  setItem(key, value) { try { window.localStorage.setItem(key, value); } catch { /* optional */ } },
  removeItem(key) { try { window.localStorage.removeItem(key); } catch { /* optional */ } },
};
