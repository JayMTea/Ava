import { useState } from 'react';
import type { Attachment, ModelInfo } from '../../lib/types';
import { Icon } from '../../lib/icons';
import { fixForCode } from '../../lib/fixes';
import { AppDot, appAccent, appForTool } from '../../lib/appColor';
import { FixLink } from './Media';
import { useBrandName } from '../../lib/brandContext';

async function copyText(s: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(s);
      return true;
    }
  } catch {
    /* fall through */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = s;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

export function UserMessage({
  text,
  atts,
  failed,
  onRetry,
}: {
  text: string;
  atts: Attachment[];
  failed?: boolean;
  onRetry?: () => void;
}) {
  return (
    <div
      className={'bubble me' + (failed ? ' failed' : '')}
      style={failed ? { cursor: 'pointer' } : undefined}
      onClick={failed ? onRetry : undefined}
    >
      {atts.length > 0 && (
        <div className="atts">
          {atts.map((a) =>
            a.kind === 'image' && a.url ? (
              <img key={a.id} src={a.url} alt={a.filename} />
            ) : (
              <div className="filepill" key={a.id}>
                <Icon name="file" />
                <span>{a.filename}</span>
              </div>
            ),
          )}
        </div>
      )}
      {text}
      {failed && (
        <div className="retry-hint">
          <Icon name="alert" />
          <span>Failed · tap to retry</span>
        </div>
      )}
    </div>
  );
}

export function AvaMessage({
  text,
  model,
  toolsUsed,
  onRetry,
  onReplay,
  children,
}: {
  text: string;
  model?: ModelInfo | null;
  toolsUsed?: string[];
  onRetry?: () => void;
  onReplay?: () => void;
  children?: React.ReactNode;
}) {
  const brand = useBrandName();
  const [copied, setCopied] = useState(false);
  return (
    <div className="bubble ava">
      {text}
      <div className="avafoot">
        {model?.label && (
          <div
            className="modelpill"
            title={`Answered by ${model.label}`}
          >
            <span className="dot" />
            <span>{model.label}</span>
          </div>
        )}
        {!!(toolsUsed && toolsUsed.length) && (
          <details className="toolsdrop">
            <summary>{`Tools used (${toolsUsed.length})`}</summary>
            <ul>
              {toolsUsed.map((t, i) => {
                // Connected-app tools carry the app's identity accent + label
                // so it's clear the capability is the app's, not Ava's.
                const app = appForTool(t);
                return (
                  <li key={`${t}:${i}`}>
                    {app && <AppDot accent={appAccent(app)} />}
                    {String(t).replace(/^.*__/, '')}
                    {app && <span className="tool-app">{app.label}</span>}
                  </li>
                );
              })}
            </ul>
          </details>
        )}
        <button type="button"
          className={'msgact' + (copied ? ' copied' : '')}
          title="Copy"
          onClick={async () => {
            if (await copyText(text)) {
              setCopied(true);
              setTimeout(() => setCopied(false), 1600);
            }
          }}
        >
          <Icon name={copied ? 'check' : 'copy'} />
          <span>{copied ? 'Copied' : 'Copy'}</span>
        </button>
        {onRetry && (
          <button type="button" className="msgact" title={`Ask ${brand} the same thing again`} onClick={onRetry}>
            <Icon name="refresh" />
            <span>Retry</span>
          </button>
        )}
        {onReplay && (
          <button type="button" className="msgact" title={`Replay ${brand}’s spoken reply`} onClick={onReplay}>
            <Icon name="mic" />
            <span>Replay</span>
          </button>
        )}
      </div>
      {children}
    </div>
  );
}

export function SysMessage({ text, icon, code }: { text: string; icon?: string; code?: string }) {
  // A coded error (e.g. "voice_off") gets a guided-fix link next to the text.
  const fix = fixForCode(code);
  return (
    <div className="bubble sys">
      {icon && (
        <span className="sysicon">
          <Icon name={icon} />
        </span>
      )}
      <span>{text}</span>
      {fix && <FixLink fix={fix} />}
    </div>
  );
}
