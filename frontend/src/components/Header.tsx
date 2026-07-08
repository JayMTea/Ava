import { Icon } from '../lib/icons';
import { ThemeToggle } from './ThemeToggle';

export function Header({
  status,
  onMenu,
  ghost,
  onToggleGhost,
  showGhost = true,
  brand = 'Ava',
  models = [],
  model = '',
  onSetModel,
}: {
  status: string;
  onMenu: () => void;
  ghost: boolean;
  onToggleGhost: () => void;
  showGhost?: boolean;
  brand?: string;
  models?: { id: string; label: string }[];
  model?: string;
  onSetModel?: (mode: string) => void;
}) {
  return (
    <header className={ghost && showGhost ? 'ghost-on' : ''}>
      <button id="menuBtn" title="Chats" aria-label="Open chats" onClick={onMenu}>
        <Icon name="sidebar" />
      </button>
      <h1>
        {brand}
        {ghost && showGhost && (
          <span className="ghost-tag">
            <Icon name="ghost" /> ghost
          </span>
        )}
      </h1>
      <div className="sub" id="status">
        {status}
      </div>
      {/* Right-side controls cluster, pinned to the top-right on every view. */}
      <div className="header-right">
        {/* Model picker — reflects the backends the user actually configured
            (ava.yaml inference.backends). Shown on the chat view when >1 brain. */}
        {showGhost && models.length > 0 && (
          <label className="model-pick" title="Which model answers">
            <Icon name="bot" />
            <select
              value={model}
              onChange={(e) => onSetModel?.(e.target.value)}
              disabled={!onSetModel || models.length < 2}
              aria-label="Select model"
            >
              {models.map((m) => (
                <option key={m.id} value={m.id}>{m.label || m.id}</option>
              ))}
            </select>
          </label>
        )}
        {showGhost && (
          <button
            className={'ghostbtn' + (ghost ? ' on' : '')}
            title={
              ghost
                ? 'Ghost mode on — private chat that is never saved. Tap to exit.'
                : 'Ghost mode — a private chat that is never saved and disappears when you leave.'
            }
            aria-label="Toggle ghost mode"
            aria-pressed={ghost}
            onClick={onToggleGhost}
          >
            <Icon name="ghost" />
          </button>
        )}
        {/* Light/dark switch — always present, far right. */}
        <ThemeToggle />
      </div>
    </header>
  );
}
