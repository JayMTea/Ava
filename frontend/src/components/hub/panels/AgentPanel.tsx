import { AGENT_SUBTABS, type AgentSubTab } from '../hubRoute';
import { MemoryPanel } from '../MemoryPanel';
import { AgentRuntimePanel } from './AgentRuntimePanel';
import { BrainPanel } from './BrainPanel';
import { PersonaPanel } from './PersonaPanel';
import { ProvidersPanel } from './ProvidersPanel';
import { SkillsPanel } from './SkillsPanel';
import { VoicePanel } from './VoicePanel';

// Agent — what Ava is: the brain it thinks with, how it talks, what it can do,
// what it remembers, how you reach it, and whether any of that has actually
// reached the sandbox yet.
//
// It is the only Setup tab with sections of its own, so it is the only nested
// router — and it owns no address state. `sub` and the badge counts arrive as
// props from HubView, which owns the whole `#hub/<tab>/<sub>` address (see
// hubRoute.ts). A second component reading the hash was the alternative, and
// two hashchange listeners racing over one URL — with a legacy rewrite in the
// middle — is the bug class the App/HubView segment split was built to avoid.
export function AgentPanel({ onRestart, sub, onSub, badges }: {
  onRestart: () => void;
  sub: AgentSubTab;
  onSub: (s: AgentSubTab) => void;
  badges: Record<string, number>;
}) {
  return (
    <>
      {/* Deliberately NOT .hub-tabs — see the block comment in hub.css. Labelled
          because a screen reader now meets two tab bars on one page and has no
          other way to tell which is which. */}
      <div className="hub-subtabs" aria-label="Agent sections">
        {AGENT_SUBTABS.map((t) => {
          const n = badges[t.id] ?? 0;
          return (
            <button
              type="button" key={t.id}
              className={'hub-subtab' + (sub === t.id ? ' active' : '')}
              aria-label={n > 0
                ? `${t.label} — ${n} change${n === 1 ? '' : 's'} waiting to reach Ava`
                : undefined}
              onClick={() => onSub(t.id)}
            >
              {t.label}
              {/* The pill is decoration; the meaning is in the aria-label. */}
              {n > 0 && <span className="hub-subtab-badge" aria-hidden="true">{n}</span>}
            </button>
          );
        })}
      </div>

      {sub === 'runtime' && <AgentRuntimePanel />}
      {sub === 'brain' && <BrainPanel onRestart={onRestart} />}
      {sub === 'persona' && <PersonaPanel />}
      {sub === 'skills' && <SkillsPanel />}
      {sub === 'memory' && <MemoryPanel />}
      {sub === 'voice' && <VoicePanel onRestart={onRestart} />}
      {sub === 'providers' && <ProvidersPanel />}
    </>
  );
}
