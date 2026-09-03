import { useEffect, useRef, useState } from 'react';
import type { ChatItem } from '../../lib/chatItems';
import { AvaMessage, SysMessage, UserMessage } from './Message';
import { ChainOfThought } from './ChainOfThought';
import { PreviewCard } from './Media';
import { Icon } from '../../lib/icons';

interface Props {
  items: ChatItem[];
  currentChatId: string | null;
  onRetryUser: (t: string, atts: import('../../lib/types').Attachment[], id: string) => void;
  onRetryAva: (t: string, atts: import('../../lib/types').Attachment[]) => void;
  onReplay: (audio: string) => void;
  onQuickSay: (t: string) => void;
  onOpenLightbox: (url: string) => void;
  onOpenArtifact: (a: import('../../lib/types').Artifact) => void;
}

function greeting(): string {
  const h = new Date().getHours();
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

// A centred hairline saying the thread's SHAPE changed — rewound, forked, or
// switched branch. One renderer for all three, because they are one visual and
// three components would be three places to keep in step.
//
// `role="separator"` rather than a paragraph: it is structural, not something
// somebody said, and a screen reader should not read it as part of the
// conversation.
function ThreadMarker({ text }: { text: string }) {
  return (
    // `aria-valuenow` is required of the FOCUSABLE separator — a splitter the
    // reader can drag to resize two panes. This is a static, unfocusable
    // divider carrying a label, so there is no value to report and inventing
    // one would be the lie the attribute exists to prevent.
    // biome-ignore lint/a11y/useAriaPropsForRole: a static divider, not a splitter
    <div className="thread-marker" role="separator" aria-label={text}>
      <span>{text}</span>
    </div>
  );
}

export function ChatView({
  items,
  currentChatId,
  onRetryUser,
  onRetryAva,
  onReplay,
  onQuickSay,
  onOpenLightbox,
  onOpenArtifact,
}: Props) {
  const logRef = useRef<HTMLDivElement>(null);
  const shouldFollowRef = useRef(true);
  const [showJump, setShowJump] = useState(false);

  function isNearBottom(el: HTMLDivElement, threshold = 96): boolean {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
  }

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    // Only auto-follow when the user is already near the bottom. If they
    // scroll up during streaming updates, do not snap them down.
    if (shouldFollowRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [items]);

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const onScroll = () => {
      shouldFollowRef.current = isNearBottom(el);
      setShowJump(!shouldFollowRef.current);
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
    return () => el.removeEventListener('scroll', onScroll);
  }, []);

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    // Switching chats should land at the latest message in that chat.
    shouldFollowRef.current = true;
    setShowJump(false);
    el.scrollTop = el.scrollHeight;
  }, [currentChatId]);

  function jumpToLatest() {
    const el = logRef.current;
    if (!el) return;
    shouldFollowRef.current = true;
    setShowJump(false);
    el.scrollTop = el.scrollHeight;
  }

  const hasConversation = items.some(
    (it) => it.kind === 'user' || it.kind === 'ava' || it.kind === 'cot',
  );

  return (
    // role="log" + aria-live: an agent turn can take 30s, and without this a
    // screen-reader user gets no announcement at all when the reply lands — the
    // whole conversation is silent to assistive tech. "polite" rather than
    // "assertive" so it queues behind whatever the user is doing, and
    // aria-relevant="additions" so appended messages announce but re-renders of
    // existing ones (a chain-of-thought step landing) do not.
    <div
      id="log"
      className="view active"
      ref={logRef}
      role="log"
      aria-live="polite"
      aria-relevant="additions"
    >
      {!hasConversation ? (
        <div className="chat-empty">
          <span className="ce-star">
            <Icon name="sparkles" />
          </span>
          <h2>{greeting()}</h2>
          <p>How can I help you today?</p>
        </div>
      ) : (
        <div className="log-inner">
          {items.map((it) => {
            switch (it.kind) {
              case 'user':
                return (
                  <UserMessage
                    key={it.id}
                    text={it.text}
                    atts={it.atts}
                    failed={it.failed}
                    onRetry={() => onRetryUser(it.text, it.atts, it.id)}
                  />
                );
              case 'ava':
                return (
                  <AvaMessage
                    key={it.id}
                    text={it.text}
                    model={it.model}
                    toolsUsed={it.toolsUsed}
                    attachments={it.attachments}
                    onRetry={it.srcText ? () => onRetryAva(it.srcText, it.srcAtts) : undefined}
                    onReplay={it.audio ? () => onReplay(it.audio!) : undefined}
                    onOpen={onOpenLightbox}
                  >
                    {it.artifact && (
                      <button type="button" className="art-chip" onClick={() => onOpenArtifact(it.artifact!)}>
                        <span>
                          <b>{it.artifact.title || 'View artifact'}</b>
                          <br />
                          <small>Open the visualization →</small>
                        </span>
                      </button>
                    )}
                  </AvaMessage>
                );
              case 'sys':
                return <SysMessage key={it.id} text={it.text} icon={it.icon} code={it.code} />;
              case 'cot':
                return (
                  <ChainOfThought
                    key={it.id}
                    label={it.label}
                    steps={it.steps}
                    status={it.status}
                    secs={it.secs}
                    error={it.error}
                    code={it.code}
                    onOpen={onOpenLightbox}
                  />
                );
              case 'preview':
                return (
                  <PreviewCard key={it.id} preview={it.preview} onOpen={onOpenLightbox} onQuickSay={onQuickSay} />
                );
              case 'marker':
                return <ThreadMarker key={it.id} text={it.text} />;
              default:
                return null;
            }
          })}
        </div>
      )}
      {showJump && (
        <button type="button" className="jump-latest" onClick={jumpToLatest}>
          Jump to latest
        </button>
      )}
    </div>
  );
}
