import { useEffect, useRef, useState } from 'react';
import type { ChatItem } from '../../lib/chatItems';
import { AvaMessage, SysMessage, UserMessage } from './Message';
import { ChainOfThought } from './ChainOfThought';
import { GenProgress, ImageMessage, PreviewCard } from './Media';
import { Icon } from '../../lib/icons';

interface Props {
  items: ChatItem[];
  currentChatId: string | null;
  onCancelGen: (jobId: string, itemId: string) => void;
  onRetryUser: (t: string, atts: import('../../lib/types').Attachment[], id: string) => void;
  onRetryAva: (t: string, atts: import('../../lib/types').Attachment[]) => void;
  onQuickSay: (t: string) => void;
  onOpenLightbox: (url: string, onClose?: () => void) => void;
  onOpenArtifact: (a: import('../../lib/types').Artifact) => void;
}

function greeting(): string {
  const h = new Date().getHours();
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

export function ChatView({
  items,
  currentChatId,
  onCancelGen,
  onRetryUser,
  onRetryAva,
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
    // scroll up during streaming/generation updates, do not snap them down.
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
    (it) => it.kind === 'user' || it.kind === 'ava' || it.kind === 'image' || it.kind === 'cot',
  );

  return (
    <div id="log" className="view active" ref={logRef}>
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
                    onRetry={it.srcText ? () => onRetryAva(it.srcText, it.srcAtts) : undefined}
                  >
                    {it.artifact && (
                      <div className="art-chip" onClick={() => onOpenArtifact(it.artifact!)}>
                        <span>
                          <b>{it.artifact.title || 'View artifact'}</b>
                          <br />
                          <small>Open the visualization →</small>
                        </span>
                      </div>
                    )}
                  </AvaMessage>
                );
              case 'sys':
                return <SysMessage key={it.id} text={it.text} icon={it.icon} />;
              case 'cot':
                return (
                  <ChainOfThought
                    key={it.id}
                    label={it.label}
                    steps={it.steps}
                    status={it.status}
                    secs={it.secs}
                    error={it.error}
                  />
                );
              case 'gen':
                return (
                  <GenProgress
                    key={it.id}
                    progress={it.progress}
                    status={it.status}
                    error={it.error}
                    prompt={it.prompt}
                    stage={it.stage}
                    elapsedSec={it.elapsedSec}
                    queueHint={it.queueHint}
                    cancelable={it.cancelable}
                    onCancel={() => onCancelGen(it.jobId, it.id)}
                  />
                );
              case 'image':
                return (
                  <ImageMessage
                    key={it.id}
                    url={it.url}
                    caption={it.caption}
                    allowUpscale={it.allowUpscale}
                    chatId={currentChatId}
                    onOpen={onOpenLightbox}
                  />
                );
              case 'preview':
                return (
                  <PreviewCard key={it.id} preview={it.preview} onOpen={(u) => onOpenLightbox(u)} onQuickSay={onQuickSay} />
                );
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
