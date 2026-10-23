import { createContext, useContext, useEffect, useState } from 'react';
import SceneBackground from './SceneBackground.jsx';
import { apiFetch } from './api.js';
import { chooseMenuBackground, localMenuTime } from './menuAppearance.js';

const MenuContext = createContext({ time: 'day', background: null });

export function MenuProvider({ children }) {
  const [time, setTime] = useState(localMenuTime);
  const [background, setBackground] = useState(null);

  useEffect(() => {
    const sync = () => setTime(localMenuTime());
    const timer = setInterval(sync, 60_000);
    window.addEventListener('focus', sync);
    document.addEventListener('visibilitychange', sync);
    return () => {
      clearInterval(timer);
      window.removeEventListener('focus', sync);
      document.removeEventListener('visibilitychange', sync);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      let candidates;
      try {
        const response = await apiFetch('/visual/menu-backgrounds', { signal: controller.signal });
        if (response.ok) candidates = (await response.json()).backgrounds;
      } catch {
        // A catalog failure still leaves a local, time-graded forest backdrop.
      }
      if (!controller.signal.aborted) setBackground(chooseMenuBackground(candidates));
    })();
    return () => controller.abort();
  }, []);

  // Keep one selection while navigating all setup screens and settings.
  return <MenuContext.Provider value={{ time, background }}>{children}</MenuContext.Provider>;
}

export function useMenuTime() {
  return useContext(MenuContext).time;
}

export function MenuFrame({ children, time: overrideTime, overlay = false }) {
  const context = useContext(MenuContext);
  const time = overrideTime || context.time, background = overlay ? null : context.background;
  return (
    <div className="setup" data-ui-time={time}>
      {!overlay && <SceneBackground background={background && { ...background, time_of_day: time }} />}
      {children}
    </div>
  );
}
