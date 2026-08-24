import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api';
import type { Artifact, Attachment, HistoryEntry } from '../lib/types';
import { ChatItem, uid } from '../lib/chatItems';
import { runPolledTurn } from './chatDirect';
import { runStreamedTurn } from './chatGateway';
import { useGateway } from './useGateway';

// Play a base64-encoded WAV (Ava's spoken reply) without a round-trip to disk.
function playWav(b64: string) {
  try {
    const bytes = atob(b64);
    const buf = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buf[i] = bytes.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
    const audio = new Audio(url);
    audio.play().catch(() => {});
    audio.onended = () => URL.revokeObjectURL(url);
  } catch {
    /* ignore playback errors */
  }
}

export function useChat() {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [chats, setChats] = useState<{ id: string; title?: string; updated?: number }[]>([]);
  const [currentChatId, setCurrentChatId] = useState<string | null>(null);
  const [pending, setPending] = useState<Attachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('hold the mic to talk');
  const [hint, setHint] = useState('');
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [ghost, setGhost] = useState(false);
  const [ctxMax, setCtxMax] = useState(65536);
  const [ctxBase, setCtxBase] = useState(2200);
  const [realCtx, setRealCtx] = useState<number | null>(null);
  const [model, setModelState] = useState('');
  const [models, setModels] = useState<{ id: string; label: string }[]>([]);
  // Set when the agent runtime is active: chat turns think with the SANDBOX
  // model and bypass the router, so the picker only steers the fallback path.
  const [agentModel, setAgentModel] = useState<string | null>(null);

  const history = useRef<HistoryEntry[]>([]);
  const chatIdRef = useRef<string | null>(null);
  const ghostRef = useRef(false);
  const ghostIdRef = useRef<string | null>(null);
  const busyRef = useRef(false);
  // The turn currently stoppable, or '' when there is nothing to stop. Only the
  // streamed path sets it — see StreamDeps.onTurnStarted.
  const [abortableTurn, setAbortableTurn] = useState('');

  const setBusyBoth = (v: boolean) => {
    busyRef.current = v;
    setBusy(v);
    if (!v) setAbortableTurn('');
  };

  const push = useCallback((it: ChatItem) => setItems((xs) => [...xs, it]), []);
  const patch = useCallback(
    (id: string, fn: (it: ChatItem) => ChatItem) => setItems((xs) => xs.map((x) => (x.id === id ? fn(x) : x))),
    [],
  );
  const remove = useCallback((id: string) => setItems((xs) => xs.filter((x) => x.id !== id)), []);
  const pushHistory = useCallback((role: string, content: string) => {
    history.current.push({ role, content } as HistoryEntry);
    history.current = history.current.slice(-12);
  }, []);

  const loadChats = useCallback(async () => {
    try {
      const j = await api.listChats();
      setChats(j.chats || []);
    } catch {
      /* ignore */
    }
  }, []);

  const setChat = (id: string | null, persist = true) => {
    chatIdRef.current = id;
    setCurrentChatId(id);
    setRealCtx(null);
    if (persist) {
      if (id) localStorage.setItem('ava.chat', id);
      else localStorage.removeItem('ava.chat');
    }
  };

  const ensureChat = useCallback(async (): Promise<string> => {
    if (ghostRef.current) {
      if (!ghostIdRef.current) ghostIdRef.current = 'ghost-' + uid();
      return ghostIdRef.current;
    }
    if (chatIdRef.current) return chatIdRef.current;
    const c = await api.newChat();
    setChat(c.id);
    loadChats();
    return c.id;
  }, [loadChats]);

  // ---- one Ava turn --------------------------------------------------------
  // The body moved to hooks/chatDirect.ts unchanged. It is the Direct floor's
  // only transport and the floor is what an unprovisioned box runs on, so it
  // was extracted rather than rewritten — see that file's header.
  //
  // WHICH PATH, and when it is decided.
  //
  // The choice is a SERVER FACT — `/api/gateway/status` says whether the
  // configured runtime has a live gateway — and it is made once, at submit, not
  // re-evaluated mid-turn. The two paths have different memory and different
  // tools; swapping them under a conversation is the worst available behaviour.
  // A gateway that drops mid-run therefore reports "reconnecting" and stays on
  // its own path rather than silently continuing somewhere else.
  const gw = useGateway();
  const [streamable, setStreamable] = useState(false);
  useEffect(() => {
    let live = true;
    const check = () => {
      void fetch('/api/gateway/status', { credentials: 'same-origin' })
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => { if (live) setStreamable(!!j?.configured && j?.phase === 'ready'); })
        // A failed check means "not streamable", never "assume yes": the floor
        // works, and guessing wrong the other way strands the turn.
        .catch(() => { if (live) setStreamable(false); });
    };
    check();
    // Provisioning can turn the gateway on; the banner already listens for this.
    window.addEventListener('ava:agent-provisioned', check);
    return () => {
      live = false;
      window.removeEventListener('ava:agent-provisioned', check);
    };
  }, []);

  const runAvaTurn = useCallback(
    (t: string, atts: Attachment[], cid: string, userItemId: string | null) => {
      // BOTH strategies submit through POST /api/chat-stream — the bridge's one
      // turn pipeline (credentials note, memory recall, chats.db history, the
      // audit record, and the same session key as the voice path). The choice
      // here is only HOW the outcome reaches the screen: live over the
      // gateway's event relay, or by polling the turn record.
      if (streamable && gw) {
        return runStreamedTurn(t, atts, cid, userItemId, {
          client: gw,
          setItems,
          setRealCtx,
          setArtifact,
          history: () => history.current,
          pushHistory,
          onTurnStarted: setAbortableTurn,
        });
      }
      return runPolledTurn(t, atts, cid, userItemId, {
        push,
        patch,
        remove,
        setItems,
        setRealCtx,
        setArtifact,
        history: () => history.current,
        pushHistory,
      });
    },
    [push, patch, remove, pushHistory, streamable, gw],
  );

  // ---- submit --------------------------------------------------------------
  const submit = useCallback(
    async (t: string, atts: Attachment[], cid: string, userItemId: string | null) => {
      try {
        await runAvaTurn(t, atts, cid, userItemId);
      } catch (e) {
        // req() folds the server's own error body into the thrown error (plus
        // a fix-it code when the backend sent one) — only a transport failure
        // is really a "network error".
        const code = (e as { code?: string }).code;
        push({ kind: 'sys', id: uid(), text: (code ? '' : 'network error: ') + (e as Error).message, icon: 'alert', code });
        if (userItemId) patch(userItemId, (it) => (it.kind === 'user' ? { ...it, failed: true } : it));
      }
    },
    [push, patch, runAvaTurn],
  );

  const send = useCallback(
    async (text: string) => {
      const t = text.trim();
      const atts = pending.slice();
      if ((!t && !atts.length) || busyRef.current) return;
      const userItemId = uid();
      push({ kind: 'user', id: userItemId, text: t, atts });
      if (t) {
        history.current.push({ role: 'user', content: t });
        history.current = history.current.slice(-12);
      }
      setPending([]);
      setBusyBoth(true);
      setStatus('thinking…');
      // try/finally, because `ensureChat` is a network call OUTSIDE the one
      // `submit` makes. If it threw — the bridge unreachable on the first send
      // of a NEW chat — setBusyBoth(false) never ran and the composer stayed
      // disabled forever; only a page reload recovered. That is reachable during
      // onboarding, because Setup itself tells you to restart Ava.
      try {
        const cid = await ensureChat();
        await submit(t, atts, cid, userItemId);
      } catch (e) {
        push({ kind: 'sys', id: uid(), icon: 'alert', text: `Couldn't reach Ava — ${(e as Error).message}` });
        if (userItemId) patch(userItemId, (it) => (it.kind === 'user' ? { ...it, failed: true } : it));
      } finally {
        setBusyBoth(false);
        setStatus('hold the mic to talk');
        loadChats();
      }
    },
    [pending, push, patch, ensureChat, submit, loadChats],
  );

  const retry = useCallback(
    async (t: string, atts: Attachment[], userItemId: string | null) => {
      if (busyRef.current) return;
      if (userItemId) patch(userItemId, (it) => (it.kind === 'user' ? { ...it, failed: false } : it));
      setBusyBoth(true);
      setStatus('thinking…');
      try {
        const cid = await ensureChat();
        await submit(t, atts, cid, userItemId);
      } catch (e) {
        push({ kind: 'sys', id: uid(), icon: 'alert', text: `Couldn't reach Ava — ${(e as Error).message}` });
        if (userItemId) patch(userItemId, (it) => (it.kind === 'user' ? { ...it, failed: true } : it));
      } finally {
        setBusyBoth(false);
        setStatus('hold the mic to talk');
        loadChats();
      }
    },
    [patch, push, ensureChat, submit, loadChats],
  );

  // ---- voice: send a recorded clip through Ava (push-to-talk) -------------
  const talk = useCallback(
    async (blob: Blob) => {
      if (busyRef.current || !blob.size) return;
      const atts = pending.slice();
      setBusyBoth(true);
      setStatus('thinking…');
      setHint('');
      try {
        const cid = await ensureChat();
        const fd = new FormData();
        fd.append('audio', blob, 'clip');
        fd.append('history', JSON.stringify(history.current));
        fd.append('attachments', JSON.stringify(atts.map((a) => a.id)));
        fd.append('chat_id', cid);
        const j = await api.talk(fd);
        if (j.error) {
          push({ kind: 'sys', id: uid(), text: j.error, icon: 'alert', code: j.error_code });
        } else if (j.accepted === false) {
          push({
            kind: 'sys',
            id: uid(),
            text: 'Voice not recognized' + (j.sim != null ? ` (match ${j.sim} < ${j.threshold})` : ''),
            icon: 'lock',
          });
        } else {
          if (j.text || atts.length) {
            push({ kind: 'user', id: uid(), text: j.text || '', atts });
            if (j.text) {
              history.current.push({ role: 'user', content: j.text });
              history.current = history.current.slice(-12);
            }
            setPending([]);
          }
          if (j.note && !j.text) push({ kind: 'sys', id: uid(), text: j.note });
          if (j.reply) {
            push({
              kind: 'ava',
              id: uid(),
              text: j.reply,
              model: j.model ?? null,
              toolsUsed: j.tools_used || [],
              srcText: j.text || '',
              srcAtts: atts,
              // Keep the spoken WAV on the item so the message's replay button
              // can play it again (session-only: not saved with chat history).
              audio: j.audio,
            });
            history.current.push({ role: 'assistant', content: j.reply });
            history.current = history.current.slice(-12);
          }
          if (j.sim != null) setHint('voice match: ' + j.sim);
          if (j.audio) playWav(j.audio);
        }
      } catch (e) {
        const code = (e as { code?: string }).code;
        push({ kind: 'sys', id: uid(), text: (code ? '' : 'network error: ') + (e as Error).message, icon: 'alert', code });
      }
      setBusyBoth(false);
      setStatus('hold the mic to talk');
      loadChats();
    },
    [pending, push, ensureChat, loadChats],
  );

  // ---- chat list / history ------------------------------------------------
  const openChat = useCallback(
    async (id: string) => {
      if (ghostRef.current) {
        const gid = ghostIdRef.current;
        ghostRef.current = false;
        ghostIdRef.current = null;
        setGhost(false);
        if (gid) api.ghostDiscard(gid);
      }
      try {
        const j = await api.getChat(id);
        setChat(id);
        history.current = [];
        const next: ChatItem[] = [];
        let lastUserText = '';
        (j.messages || []).forEach((m) => {
          if (m.role === 'user') {
            lastUserText = m.content || '';
            next.push({ kind: 'user', id: uid(), text: m.content || '', atts: m.atts || [] });
          } else if (m.error_code) {
            // A turn that ended in a coded failure — replay it as the same
            // fix-it system line the live UI would have shown.
            next.push({ kind: 'sys', id: uid(), text: m.content || 'failed', icon: 'alert', code: m.error_code });
          } else {
            // Durable chain-of-thought: replay the saved reasoning above the
            // reply, exactly as it appeared live (collapsed, status done).
            if (m.steps && m.steps.length) {
              next.push({
                kind: 'cot', id: uid(), label: 'Reasoning',
                steps: m.steps, status: 'done',
              });
            }
            next.push({
              kind: 'ava',
              id: uid(),
              text: m.content || '',
              model: m.model || null,
              toolsUsed: m.tools_used || [],
              srcText: lastUserText,
              srcAtts: m.atts || [],
            });
          }
        });
        if (!next.length) {
          next.push({ kind: 'sys', id: uid(), text: 'New chat. Type a message or attach a file.' });
        }
        setItems(next);
      } catch {
        /* ignore */
      }
    },
    [],
  );

  const newChat = useCallback(async () => {
    if (ghostRef.current) {
      const gid = ghostIdRef.current;
      ghostRef.current = false;
      ghostIdRef.current = null;
      setGhost(false);
      if (gid) api.ghostDiscard(gid);
    }
    try {
      const c = await api.newChat();
      setChat(c.id);
      history.current = [];
      setItems([{ kind: 'sys', id: uid(), text: 'New chat. Type a message or attach a file.' }]);
      loadChats();
    } catch {
      /* ignore */
    }
  }, [loadChats]);

  // ---- ghost mode: ephemeral, unsaved conversation ------------------------
  // Uses an unregistered chat id, so nothing is written to chats.json (the
  // append is a no-op) and it never shows in the sidebar. Ava still gets a real
  // OpenClaw session, so she has full multi-turn context + every tool. Leaving
  // ghost (toggle off / new chat / open chat) wipes the session transcript too.
  const toggleGhost = useCallback(async () => {
    if (ghostRef.current) {
      await newChat();
      return;
    }
    ghostRef.current = true;
    ghostIdRef.current = 'ghost-' + uid();
    setGhost(true);
    setChat(ghostIdRef.current, false);
    history.current = [];
    setItems([
      {
        kind: 'sys',
        id: uid(),
        text: "Ghost mode is on \u2014 this conversation is private and won't be saved. It's erased when you turn ghost off or start a new chat.",
        icon: 'ghost',
      },
    ]);
  }, [newChat]);

  const deleteChat = useCallback(
    async (id: string) => {
      if (!window.confirm('Delete this chat? This cannot be undone.')) return;
      try {
        await api.deleteChat(id);
        if (id === chatIdRef.current) setChat(null);
        await loadChats();
        if (!chatIdRef.current) {
          const j = await api.listChats();
          if (j.chats?.length) openChat(j.chats[0].id);
          else newChat();
        }
      } catch {
        /* ignore */
      }
    },
    [loadChats, openChat, newChat],
  );

  // ---- attachments --------------------------------------------------------
  const uploadFiles = useCallback(async (fileList: FileList) => {
    const files = [...fileList];
    if (!files.length) return;
    setHint('uploading…');
    try {
      const j = await api.upload(files);
      const good: Attachment[] = [];
      (j.attachments || []).forEach((a) => {
        if (a.error) setItems((xs) => [...xs, { kind: 'sys', id: uid(), text: a.error!, icon: 'alert' }]);
        else good.push(a);
      });
      setPending((p) => [...p, ...good]);
      setHint('');
    } catch (e) {
      setHint('');
      setItems((xs) => [...xs, { kind: 'sys', id: uid(), text: 'upload failed: ' + (e as Error).message, icon: 'alert' }]);
    }
  }, []);

  const removeAtt = useCallback((id: string) => setPending((p) => p.filter((a) => a.id !== id)), []);

  const quickSay = useCallback((t: string) => send(t), [send]);

  const refreshArtifact = useCallback(async () => {
    if (!artifact || artifact.type !== 'weather') return;
    try {
      const j = await api.weatherArtifact(artifact.location || '', (artifact.daily || []).length || 7);
      if (j && j.type) setArtifact(j);
    } catch {
      /* ignore */
    }
  }, [artifact]);

  // ---- initial load -------------------------------------------------------
  useEffect(() => {
    (async () => {
      const last = localStorage.getItem('ava.chat');
      try {
        const j = await api.listChats();
        setChats(j.chats || []);
        const ids = (j.chats || []).map((c) => c.id);
        if (last && ids.includes(last)) openChat(last);
        else if (ids.length) openChat(ids[0]);
        else setItems([{ kind: 'sys', id: uid(), text: 'Type a message or attach a document or image.' }]);
      } catch {
        setItems([{ kind: 'sys', id: uid(), text: 'Type a message or attach a document or image.' }]);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- context sizing from the bridge -------------------------------------
  useEffect(() => {
    api
      .health()
      .then((h) => {
        if (h.ctx_max) setCtxMax(h.ctx_max);
        if (typeof h.ctx_base === 'number') setCtxBase(h.ctx_base);
      })
      .catch(() => {});
  }, []);

  // ---- model picker (which brain Ava uses) --------------------------------
  const loadModel = useCallback(() => {
    api
      .getModel()
      .then((r) => {
        if (r.mode) setModelState(r.mode);
        if (r.backends) {
          setModels(r.backends.map((b) => ({
            id: b.id,
            // Declared by AVA_BACKEND_URL rather than in Ava's own config —
            // still the owner's choice, just not one made here. It read
            // "(default)" while Ava still shipped a model of its own; it does
            // not, so "default" would now name something that cannot exist.
            label: b.implicit ? `${b.label} (from environment)` : b.label,
          })));
        }
        setAgentModel(r.agent_model || null);
      })
      .catch(() => {});
  }, []);
  useEffect(() => { loadModel(); }, [loadModel]);
  // This was a one-shot fetch on mount, so after a provision changed the sandbox
  // model the header kept naming a model Ava no longer used until a page reload.
  // An event rather than a poll: the value changes maybe twice a year, and this
  // keeps useChat from importing hub code. Same idiom as `ava:apps-changed`.
  useEffect(() => {
    window.addEventListener('ava:agent-provisioned', loadModel);
    return () => window.removeEventListener('ava:agent-provisioned', loadModel);
  }, [loadModel]);

  const setModelMode = useCallback(async (mode: string) => {
    setModelState(mode);
    try {
      const r = await api.setModel(mode);
      if (r && r.mode) setModelState(r.mode);
    } catch {
      /* ignore */
    }
  }, []);

  // Best-effort wipe of a ghost session if the tab closes mid-ghost.
  useEffect(() => {
    const onUnload = () => {
      if (ghostRef.current && ghostIdRef.current) {
        const fd = new FormData();
        fd.append('chat_id', ghostIdRef.current);
        navigator.sendBeacon?.('/api/ghost/discard', fd);
      }
    };
    window.addEventListener('beforeunload', onUnload);
    return () => window.removeEventListener('beforeunload', onUnload);
  }, []);

  // ---- context-window token estimate (per chat) ---------------------------
  const ctxTokens = useMemo(() => {
    if (realCtx != null) return realCtx;
    let chars = 0;
    for (const it of items) {
      if (it.kind === 'user' || it.kind === 'ava') chars += (it.text || '').length;
    }
    return ctxBase + Math.ceil(chars / 4);
  }, [items, ctxBase, realCtx]);

  // Replay a stored voice reply (base64 WAV) from a message's replay button.
  const replay = useCallback((b64: string) => playWav(b64), []);

  /** Ask the bridge to stop the turn in flight.
   *
   *  Does NOT clear `busy` or write anything into the transcript. The run's
   *  ending arrives through the same path every other ending does — the
   *  gateway reports it with `aborted` set, and the turn reaches its terminal
   *  status there. Optimistically ending the turn here would leave the screen
   *  disagreeing with the record whenever the abort did not land.
   */
  const stop = useCallback(async () => {
    const tid = abortableTurn;
    if (!tid) return;
    setAbortableTurn('');          // one ask per turn; the button goes quiet
    try {
      const r = await api.abortTurn(tid);
      if (!r?.ok && r?.error) {
        push({ kind: 'sys', id: uid(), text: r.error, icon: 'alert',
               code: r.code });
      }
    } catch {
      /* the turn ends on its own terms either way; a failed ask is not worth
         a second error on top of whatever the run is about to report */
    }
  }, [abortableTurn, push]);

  return {
    items,
    chats,
    currentChatId,
    pending,
    busy,
    status,
    hint,
    artifact,
    setArtifact,
    send,
    stop,
    canStop: !!abortableTurn,
    retry,
    replay,
    talk,
    openChat,
    newChat,
    deleteChat,
    uploadFiles,
    removeAtt,
    quickSay,
    refreshArtifact,
    loadChats,
    ghost,
    toggleGhost,
    ctxTokens,
    ctxMax,
    model,
    models,
    agentModel,
    setModelMode,
  };
}
