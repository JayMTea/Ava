import { useEffect, useState } from 'react';
import { Icon } from '../lib/icons';
import { getTheme, toggleTheme, type Theme } from '../lib/theme';

// Top-bar light/dark switch. Stateless beyond the current theme — all the color
// work lives in tokens.css, so this button just flips the <html data-theme>.
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => getTheme());

  // Stay in sync if the theme changes elsewhere (another tab, or future callers).
  useEffect(() => {
    const onChange = () => setTheme(getTheme());
    window.addEventListener('ava:theme', onChange);
    window.addEventListener('storage', onChange);
    return () => {
      window.removeEventListener('ava:theme', onChange);
      window.removeEventListener('storage', onChange);
    };
  }, []);

  const next = theme === 'dark' ? 'light' : 'dark';
  return (
    <button type="button"
      className="theme-btn"
      title={`Switch to ${next} mode`}
      aria-label={`Switch to ${next} mode`}
      onClick={() => setTheme(toggleTheme())}
    >
      <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
    </button>
  );
}
