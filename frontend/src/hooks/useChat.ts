import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, sleep } from '../lib/api';
import type { Artifact, Attachment, HistoryEntry } from '../lib/types';
import { ChatItem, uid } from '../lib/chatItems';

// Routing lives SERVER-SIDE now (ava_bridge/turn_router.py): every typed
// message posts to /api/chat-stream, which answers {turn_id} for an agent turn
// or {job} for a render. The old client-side GEN_RE heuristic (and the
// exception list it kept accreting) is retired — this file carries zero
// routing knowledge.

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

  const setBusyBoth = (v: boolean) => {
    busyRef.current = v;
    setBusy(v);
  };

  const push = useCallback((it: ChatItem) => setItems((xs) => [...xs, it]), []);
  const patch = useCallback(
    (id: string, fn: (it: ChatItem) => ChatItem) => setItems((xs) => xs.map((x) => (x.id === id ? fn(x) : x))),
    [],
  );
  const remove = useCallback((id: string) => setItems((xs) => xs.filter((x) => x.id !== id)), []);

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

  // ---- GPU workloads job polling ---------------------------------------
  const pollJob = useCallback(
    (job: { id: string; prompt?: string }, chatId: string | null) => {
      const gid = uid();
      push({
        kind: 'gen',
        id: gid,
        jobId: job.id,
        progress: 0,
        status: 'running',
        prompt: job.prompt,
        stage: 'queued',
        elapsedSec: 0,
        queueHint: 'queued',
        cancelable: true,
      });
      const t0 = Date.now();
      // Wall-clock deadline + error budget so a stuck server-side job can't
      // spin the gen bar forever (the server reaper flips stalled jobs to
      // error, but the client must also self-terminate if unreachable).
      const JOB_DEADLINE_MS = 20 * 60 * 1000;
      let pollFails = 0;
      const giveUp = (why: string) =>
        patch(gid, (it) =>
          it.kind === 'gen'
            ? { ...it, status: 'error', error: why, cancelable: false, stage: 'error',
                elapsedSec: (Date.now() - t0) / 1000 }
            : it,
        );
      const tick = async () => {
        if (Date.now() - t0 > JOB_DEADLINE_MS) {
          giveUp('render timed out — check the the GPU service service on the Operations page');
          return;
        }
        try {
          const j = await api.job(job.id);
          pollFails = 0;
          if (j.status === 'running') {
            const elapsedSec = (Date.now() - t0) / 1000;
            const pct = j.progress || 0;
            let queueHint = '';
            let stage = j.stage || (pct <= 0 ? 'queued' : pct >= 75 ? 'upscaling' : 'rendering');
            if (!j.stage && pct <= 0) {
              queueHint = elapsedSec < 25 ? 'queued / loading model' : 'still queued';
            }
            patch(gid, (it) =>
              it.kind === 'gen'
                ? {
                    ...it,
                    progress: pct,
                    elapsedSec,
                    stage,
                    queueHint,
                    cancelable: true,
                    prompt: j.rewritten_prompt || j.prompt || it.prompt,
                  }
                : it,
            );
            setTimeout(tick, 700);
          } else if (j.status === 'done' && j.url) {
            remove(gid);
            push({ kind: 'image', id: uid(), url: j.url, caption: job.prompt, allowUpscale: true });
            // The bridge already persisted the image to the chat when the job
            // finished (gpu_jobs._finalize_job) — just refresh the list.
            if (chatId) loadChats();
          } else {
            patch(gid, (it) =>
              it.kind === 'gen'
                ? {
                    ...it,
                    status: 'error',
                    error: j.error || 'unknown',
                    errorCode: j.error_code,
                    cancelable: false,
                    stage: j.stage || 'error',
                    elapsedSec: (Date.now() - t0) / 1000,
                  }
                : it,
            );
          }
        } catch {
          pollFails += 1;
          if (pollFails > 20) {
            giveUp('lost contact with the server while rendering');
            return;
          }
          setTimeout(tick, 1500);
        }
      };
      tick();
    },
    [push, patch, remove, loadChats],
  );

  const cancelGen = useCallback(
    async (jobId: string, itemId: string) => {
      try {
        await api.cancelJob(jobId);
      } catch {
        // Even if the request races/fails, reflect user intent in UI.
      }
      patch(itemId, (it) =>
        it.kind === 'gen'
          ? {
              ...it,
              status: 'error',
              error: 'cancelled by user',
              cancelable: false,
              stage: 'cancelled',
            }
          : it,
      );
    },
    [patch],
  );

  // ---- one Ava turn: chain-of-thought + poll ------------------------------
  const runAvaTurn = useCallback(
    async (t: string, atts: Attachment[], cid: string, userItemId: string | null) => {
      const cotId = uid();
      push({
        kind: 'cot',
        id: cotId,
        label: atts.length ? 'Ava is reading & thinking' : 'Ava is thinking',
        steps: [],
        status: 'running',
      });
      const t0 = Date.now();
      const failUser = () => {
        if (userItemId) patch(userItemId, (it) => (it.kind === 'user' ? { ...it, failed: true } : it));
      };
      try {
        const fd = new FormData();
        fd.append('text', t);
        fd.append('history', JSON.stringify(history.current.slice(0, -1)));
        fd.append('attachments', JSON.stringify(atts.map((a) => a.id)));
        fd.append('chat_id', cid);
        const start = await api.startTurn(fd);
        if (start.job) {
          // The server-side intent gate routed this to the image pipeline.
          remove(cotId);
          pollJob(start.job, cid);
          return;
        }
        if (!start.turn_id) {
          remove(cotId);
          push({ kind: 'sys', id: uid(), text: start.error || 'could not start', icon: 'alert', code: start.error_code });
          failUser();
          return;
        }
        // Bound the poll loop so a stuck turn can never spin forever (the old
        // `for(;;)` would leave "Ava is thinking" up indefinitely on a hang, which
        // read as "no response"). Show honest elapsed-time hints while we wait,
        // and after DEADLINE_MS give up cleanly with a retryable error.
        const DEADLINE_MS = 200_000;
        let pollFails = 0;
        for (;;) {
          await sleep(750);
          const waited = Date.now() - t0;
          if (waited > DEADLINE_MS) {
            patch(cotId, (it) =>
              it.kind === 'cot'
                ? { ...it, status: 'error', error: 'Ava took too long to respond — please try again.' }
                : it,
            );
            failUser();
            return;
          }
          let s;
          try {
            s = await api.turn(start.turn_id);
            pollFails = 0;
          } catch {
            // Tolerate transient network blips, but don't poll a dead server forever.
            if (++pollFails > 20) {
              patch(cotId, (it) => (it.kind === 'cot' ? { ...it, status: 'error', error: 'lost connection to Ava' } : it));
              failUser();
              return;
            }
            continue;
          }
          if (s.steps) patch(cotId, (it) => (it.kind === 'cot' ? { ...it, steps: s.steps! } : it));
          // Keep the user informed while a slow turn is still working.
          if (s.status === 'running') {
            const secs = Math.round(waited / 1000);
            const label =
              secs >= 45 ? `Still working… (${secs}s)` : secs >= 20 ? 'Working on it…' : atts.length ? 'Ava is reading & thinking' : 'Ava is thinking';
            patch(cotId, (it) => (it.kind === 'cot' ? { ...it, label } : it));
          }
          if (s.status === 'done') {
            const secs = Math.round((Date.now() - t0) / 1000);
            patch(cotId, (it) => (it.kind === 'cot' ? { ...it, status: 'done', secs } : it));
            if (typeof s.ctx_tokens === 'number') setRealCtx(s.ctx_tokens);
            if (s.reply) {
              push({
                kind: 'ava',
                id: uid(),
                text: s.reply,
                model: s.model,
                toolsUsed: s.tools_used || [],
                artifact: s.artifact ?? null,
                srcText: t,
                srcAtts: atts,
              });
              history.current.push({ role: 'assistant', content: s.reply });
              history.current = history.current.slice(-12);
            }
            if (s.job) pollJob(s.job, cid);
            if (s.artifact) setArtifact(s.artifact);
            if (s.previews?.length) s.previews.forEach((p) => push({ kind: 'preview', id: uid(), preview: p }));
            return;
          }
          if (s.status === 'error' || (!s.status && s.error)) {
            patch(cotId, (it) => (it.kind === 'cot' ? { ...it, status: 'error', error: s.error || 'failed' } : it));
            failUser();
            return;
          }
        }
      } catch {
        patch(cotId, (it) => (it.kind === 'cot' ? { ...it, status: 'error', error: 'network error' } : it));
        failUser();
      }
    },
    [push, patch, remove, pollJob],
  );

  // ---- submit: one path — the server-side gate picks the pipeline ----------
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
    async (text: string, codeMode: boolean) => {
      const t = text.trim();
      const atts = pending.slice();
      if ((!t && !atts.length) || busyRef.current) return;
      if (codeMode) {
        if (!t) return;
        push({ kind: 'user', id: uid(), text: t, atts: [] });
        push({
          kind: 'sys',
          id: uid(),
          // Do NOT send people to /legacy: that panel POSTs to /api/code/turn,
          // which no longer exists, so following the old advice ended in a 404.
          // Governed self-editing runs from the Control Center.
          text: 'Code mode isn’t wired to the composer. Ava’s governed self-editing runs from Operations → Control Center, where each change waits for your approval.',
          icon: 'code',
        });
        return;
      }
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
          if (j.job) pollJob(j.job, cid);
        }
      } catch (e) {
        const code = (e as { code?: string }).code;
        push({ kind: 'sys', id: uid(), text: (code ? '' : 'network error: ') + (e as Error).message, icon: 'alert', code });
      }
      setBusyBoth(false);
      setStatus('hold the mic to talk');
      loadChats();
    },
    [pending, push, ensureChat, pollJob, loadChats],
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
          } else if (m.image) {
            next.push({ kind: 'image', id: uid(), url: m.image, caption: m.content || '', allowUpscale: true });
          } else if (m.error_code) {
            // A render (or other job) that ended in a coded failure — replay it
            // as the same fix-it system line the live UI would have shown.
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

  const quickSay = useCallback((t: string) => send(t, false), [send]);

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
  useEffect(() => {
    api
      .getModel()
      .then((r) => {
        if (r.mode) setModelState(r.mode);
        if (r.backends) {
          setModels(r.backends.map((b) => ({
            id: b.id,
            // A built-in/env default is not a user-chosen backend — say so.
            label: b.implicit ? `${b.label} (default)` : b.label,
          })));
        }
        setAgentModel(r.agent_model || null);
      })
      .catch(() => {});
  }, []);

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

  return {
    items,
    cancelGen,
    chats,
    currentChatId,
    pending,
    busy,
    status,
    hint,
    artifact,
    setArtifact,
    send,
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
