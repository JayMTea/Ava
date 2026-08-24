import { useEffect, useState } from 'react';
import { agentApi } from '../../lib/agentApi';
import { useGateway } from '../../hooks/useGateway';
import { EmptyState } from '../dashboard/layout';

// A terminal, or an honest account of why there isn't one.
//
// WHY THIS PANEL DOES NOT RENDER AN XTERM
// ---------------------------------------
// It used to. It never worked. `terminal.open` was called with a `sessionId`
// the strict schema refuses, the handle was read from a `terminalId` field the
// gateway has never had, and output was read from a `terminal.output` event
// that NOTHING emits — not the gateway's 218 methods, not the bridge relay,
// not the fake. That last one is the whole reason this class of bug survived:
// an unadvertised METHOD fails loud, but subscribing to a topic nobody sends
// is silent, so the panel presented a black rectangle that simply never
// printed anything, which reads as "my shell is quiet" rather than "this is
// broken".
//
// The params are now captured from the live gateway. The OUTPUT MECHANISM is
// not, and cannot be here: `gateway.terminal.enabled` is off on this sandbox,
// so `terminal.open` refuses and there is no session to observe. `terminal.
// text` -> {sessionId} exists and looks like a buffer read, but wiring a
// terminal on "looks like" is precisely what produced the six invented names.
//
// So the panel does the one thing that is true on every gateway: it probes,
// and it reports. On a gateway with terminal off (this one) that is the real
// reason, which is actionable. On a gateway with terminal ON it says the
// wiring is unfinished rather than opening a dead rectangle — and it closes
// the session it opened, because a probe that leaks a shell per mount is
// worse than no probe.
export function TerminalPanel({ sessionId }: { sessionId: string }) {
  const client = useGateway();
  const [state, setState] = useState<'probing' | 'unavailable' | 'unfinished'>('probing');
  const [reason, setReason] = useState('');

  useEffect(() => {
    let live = true;
    if (!client) return;
    setState('probing');
    setReason('');
    const api = agentApi(client);
    // 80x24 is the classic default and only has to be VALID — cols/rows are
    // required ints, and this call exists to learn whether it is refused.
    void api.terminal.open(80, 24)
      .then((got) => {
        const tid = got?.sessionId || got?.id || null;
        // Opened for the probe only; nothing here can drive it yet.
        if (tid) void api.terminal.close(tid).catch(() => {});
        if (live) setState('unfinished');
      })
      .catch((e) => {
        if (!live) return;
        setReason((e as Error).message || String(e));
        setState('unavailable');
      });
    return () => { live = false; };
  }, [client, sessionId]);

  if (state === 'probing') return <EmptyState text="Checking for a terminal…" />;

  if (state === 'unavailable') {
    return (
      <div className="agent-term-err">
        <p className="hub-msg err">{reason}</p>
        <p className="hub-note">
          A terminal needs <code>gateway.terminal.enabled</code> on the gateway
          and <code>operator.admin</code> on the token Ava connects with. Both
          are gateway-side settings — Ava cannot turn them on for you.
        </p>
      </div>
    );
  }

  return (
    <div className="agent-term-err">
      <p className="hub-msg">This gateway allows terminals; Ava&rsquo;s wiring
        isn&rsquo;t finished.</p>
      <p className="hub-note">
        Opening, input and resize are wired to the gateway&rsquo;s real schema,
        but how output is delivered has not been confirmed against a running
        terminal, and Ava does not ship a guess. Nothing was left open.
      </p>
    </div>
  );
}
