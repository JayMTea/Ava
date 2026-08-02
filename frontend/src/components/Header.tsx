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
  agentModel = null,
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
  agentModel?: string | null;
  onSetModel?: (mode: string) => void;
}) {
  return (
    <header className={ghost && showGhost ? 'ghost-on' : ''}>
      <button type="button" id="menuBtn" title="Chats" aria-label="Open chats" onClick={onMenu}>
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
        {/* Model picker — the router backends. With the agent runtime active,
            turns think with the SANDBOX model and bypass the router, so this
            control only steers the tool-less fallback path — the title must
            not promise "which model answers" when it doesn't. */}
        {/* With the agent live this was a <select> that steered a path nothing
            was using: every option was suffixed "· fallback" and the tooltip
            spent 25 words explaining that the obvious reading was wrong. Three
            apologies for one control. A <select> is a promise; when the thing it
            promises isn't what happens, don't ship the control — name the model
            that IS answering and point at where it's changed. The fallback stays
            fully configurable in Setup → Agent, which already manages exactly
            that. */}
        {showGhost && agentModel ? (
          <a
            className="model-pick model-pick-static"
            data-tour="model-chip"
            href="#hub/agent/brain"
            title={`Ava answers with ${agentModel.split('/').pop()} in the agent sandbox. Change it in Setup → Agent → Brain.`}
          >
            <Icon name="bot" />
            <span>{agentModel.split('/').pop()}</span>
          </a>
        ) : showGhost && models.length > 0 ? (
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
        ) : null}
        {showGhost && (
          <button type="button"
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
