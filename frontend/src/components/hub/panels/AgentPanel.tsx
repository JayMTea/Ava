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
// router — and it owns no address state. `sub` arrives as a prop from HubView,
// which owns the whole `#hub/<tab>/<sub>` address (see hubRoute.ts). A second
// component reading the hash was the alternative, and two hashchange listeners
// racing over one URL — with a legacy rewrite in the middle — is the bug class
// the App/HubView segment split was built to avoid.
export function AgentPanel({ onRestart, sub, onSub }: {
  onRestart: () => void;
  sub: AgentSubTab;
  onSub: (s: AgentSubTab) => void;
}) {
  return (
    <>
      {/* Deliberately NOT .hub-tabs — see the block comment in hub.css. Labelled
          because a screen reader now meets two tab bars on one page and has no
          other way to tell which is which. */}
      <p className="agent-setup-intro">Check your runtime, choose a brain, then tailor your agent’s skills and memory.</p>
      <div className="hub-subtabs" role="tablist" aria-label="Agent sections">
        {AGENT_SUBTABS.map((t) => (
          <button
            type="button" key={t.id}
            role="tab" id={`agent-tab-${t.id}`}
            aria-selected={sub === t.id} aria-controls={`agent-section-${sub}`}
            tabIndex={sub === t.id ? 0 : -1}
            className={'hub-subtab' + (sub === t.id ? ' active' : '')}
            onClick={() => onSub(t.id)}
            onKeyDown={(e) => {
              const index = AGENT_SUBTABS.findIndex((tab) => tab.id === t.id);
              const next = e.key === 'ArrowRight' ? (index + 1) % AGENT_SUBTABS.length
                : e.key === 'ArrowLeft' ? (index - 1 + AGENT_SUBTABS.length) % AGENT_SUBTABS.length
                  : e.key === 'Home' ? 0 : e.key === 'End' ? AGENT_SUBTABS.length - 1 : -1;
              if (next < 0) return;
              e.preventDefault();
              const id = AGENT_SUBTABS[next].id;
              onSub(id);
              document.getElementById(`agent-tab-${id}`)?.focus();
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="agent-setup-panels" role="tabpanel" id={`agent-section-${sub}`}
        aria-labelledby={`agent-tab-${sub}`} tabIndex={0}>
      {sub === 'runtime' && <AgentRuntimePanel />}
      {sub === 'brain' && <BrainPanel onRestart={onRestart} />}
      {sub === 'persona' && <PersonaPanel />}
      {sub === 'skills' && <SkillsPanel />}
      {sub === 'memory' && <MemoryPanel />}
      {sub === 'voice' && <VoicePanel onRestart={onRestart} />}
      {sub === 'providers' && <ProvidersPanel />}
      </div>
    </>
  );
}
