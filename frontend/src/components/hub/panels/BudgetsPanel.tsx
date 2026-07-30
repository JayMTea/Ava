import { useEffect, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { EmptyState, Panel } from '../../dashboard/primitives';
import { ResourceError } from '../ui/ResourceState';
import { useAction, useResource } from '../hooks';
import { hub } from '../hubApi';
import type { CostSettings } from '../hubApi';
import { HubMessage } from '../ui/HubMessage';
import { Legend } from '../ui/Legend';

// Budgets — spend/energy caps + live meter.
function meterTone(pct: number): 'ok' | 'warn' | 'err' {
  return pct >= 100 ? 'err' : pct >= 80 ? 'warn' : 'ok';
}

const fmtMoney = (n: number) => `$${n.toFixed(2)}`;
const fmtCap = (n: number, unit: '$' | 'kWh') => (unit === '$' ? `$${n}` : `${n} kWh`);

// A budget meter row: usage against a cap with a live track, remaining, and %.
// Unlike the old bar it ALWAYS renders — so you see today's spend/energy even
// before any cap is set (the track just reads "no cap set"), and the energy row
// converts kWh to money at your rate so the two costs read in the same terms.
function BudgetMeter({ label, used, cap, unit, rate }: {
  label: string; used: number | null; cap: number | null; unit: '$' | 'kWh'; rate?: number;
}) {
  // `used` is null when the figure is genuinely unavailable (no power sensor and
  // no declared wattage). Dividing that by a cap yields NaN and a meter that
  // renders as a full bar — worse than showing nothing.
  const has = cap != null && cap > 0 && used != null;
  const ratio = has ? (used as number) / (cap as number) : 0;
  const pct = has ? Math.min(100, Math.round(ratio * 100)) : 0;
  const tone = has ? meterTone(ratio * 100) : 'muted';
  const fmt = (n: number) => (unit === '$' ? fmtMoney(n) : `${n.toFixed(2)} kWh`);
  const remaining = has ? Math.max(0, (cap as number) - used) : 0;
  const energyCost = unit === 'kWh' && rate && used != null ? used * rate : null;
  // Capped meters carry a tone; an uncapped one carries none so its value falls
  // back to txt and its (zero-width) fill to no colour.
  const toneClass = has ? `tone-${tone}` : '';
  return (
    <div className="bud-meter">
      <div className="bud-meter-head">
        <span className="bud-meter-label">{label}</span>
        <span className="bud-meter-val">
          {/* An unavailable figure shows the same em dash as any other unknown
              metric — never 0, which would read as "you used no electricity". */}
          <b className={toneClass}>{used == null ? '—' : fmt(used)}</b>
          <span className="bud-cap">{has ? ` / ${fmtCap(cap as number, unit)}` : ' · no cap set'}</span>
        </span>
      </div>
      <div className="bud-track" aria-hidden="true">
        <div className={`bud-fill ${toneClass}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="bud-meter-foot">
        <span>{energyCost != null ? `≈ ${fmtMoney(energyCost)} today at your rate` : ' '}</span>
        <span>{has ? `${fmt(remaining)} left · ${pct}%` : 'set a cap below to meter it'}</span>
      </div>
    </div>
  );
}

export function BudgetsPanel() {
  const costRes = useResource(() => hub.cost());
  const { data: c, reload } = costRes;
  const [rate, setRate] = useState('');
  const [du, setDu] = useState('');
  const [mu, setMu] = useState('');
  const [dk, setDk] = useState('');
  const { busy, message, run } = useAction();

  // Seed the editable fields from the loaded settings (and re-seed after a save).
  useEffect(() => {
    if (!c) return;
    setRate(String(c.electricity_rate_per_kwh ?? ''));
    setDu(c.budgets.daily_usd != null ? String(c.budgets.daily_usd) : '');
    setMu(c.budgets.monthly_usd != null ? String(c.budgets.monthly_usd) : '');
    setDk(c.budgets.daily_kwh != null ? String(c.budgets.daily_kwh) : '');
  }, [c]);

  const save = () => run(async () => {
    const num = (s: string) => (s.trim() === '' ? null : Number(s));
    const r = await hub.saveCost({
      electricity_rate_per_kwh: rate.trim() === '' ? 0 : Number(rate),
      budgets: { daily_usd: num(du), monthly_usd: num(mu), daily_kwh: num(dk) },
    } as Partial<CostSettings>);
    if (r.error) return r.error;
    reload();
  }, 'Saved.');

  const anyCap = c && (c.budgets.daily_usd != null || c.budgets.monthly_usd != null || c.budgets.daily_kwh != null);

  return (
    <>
      <ResourceError r={costRes} label="your cost settings" />
      <Panel title="Today's spend & energy" subtitle="Live usage against your caps. Cloud API calls cost real money; local generation only costs electricity.">
        {c ? (
          <>
            <BudgetMeter label="Cloud spend today" used={c.daily_spend_usd} cap={c.budgets.daily_usd} unit="$" />
            <BudgetMeter
              label={`GPU energy today${c.daily_energy_kwh == null ? ' (not measured)'
                : c.power_measured ? '' : ' (est.)'}`}
              used={c.daily_energy_kwh} cap={c.budgets.daily_kwh} unit="kWh"
              rate={c.electricity_rate_per_kwh} />
            {c.budgets.monthly_usd != null && (
              <div className="bud-monthly">
                <Icon name="calendar" />
                <span>Monthly cloud cap <b>{fmtMoney(c.budgets.monthly_usd)}</b> — alerts fire at 80% and 100% on the <b>Operations</b> page.</span>
              </div>
            )}
            {!anyCap && (
              <div className="bud-monthly">
                <Icon name="info" />
                <span>No caps set yet — usage is shown above; add a cap below to turn on the meter and alerts.</span>
              </div>
            )}
          </>
        ) : <EmptyState text="Loading…" />}
      </Panel>

      <div className="hub-section" />
      <Panel title="Set budgets" subtitle="Blank = that budget is off. Nothing is ever blocked — caps only drive alerts on the Operations page.">
        <div className="hub-field" style={{ maxWidth: 340 }}>
          <label>Electricity rate ({c?.currency || '$'} per kWh)</label>
          <input className="hub-input" value={rate} onChange={(e) => setRate(e.target.value)} placeholder="0.15" inputMode="decimal" />
          <div className="bud-field-hint">Turns GPU energy into a dollar figure. Leave at 0 to show energy in kWh only.</div>
        </div>
        <div className="bud-caps-head">Caps <span>— leave blank to disable</span></div>
        <div className="hub-fieldrow">
          <div className="hub-field"><label>Daily cloud spend</label>
            <input className="hub-input" value={du} onChange={(e) => setDu(e.target.value)} placeholder="none" inputMode="decimal" /></div>
          <div className="hub-field"><label>Monthly cloud spend</label>
            <input className="hub-input" value={mu} onChange={(e) => setMu(e.target.value)} placeholder="none" inputMode="decimal" /></div>
          <div className="hub-field"><label>Daily energy (kWh)</label>
            <input className="hub-input" value={dk} onChange={(e) => setDk(e.target.value)} placeholder="none" inputMode="decimal" /></div>
        </div>
        <div className="hub-btn-row">
          <button className="hub-btn" onClick={save} disabled={busy}><Icon name="check" />{busy ? 'Saving…' : 'Save budgets'}</button>
        </div>
        <HubMessage message={message} />

        <Legend
          title="How budgets work"
          items={[
            { icon: 'cloud', term: 'Cloud $', desc: 'Real API spend — only calls to a cloud model cost money. Everything Ava runs locally is free.' },
            { icon: 'gauge', term: 'Local energy', desc: `GPU electricity at your rate${c && !c.power_measured ? ', estimated from GPU load until a power meter is present' : ''}.` },
            { icon: 'alert', term: 'Alerts, not blocks', desc: <>Nothing is ever stopped. Hitting a cap raises an alert on the <b>Operations</b> page at 80% and 100%.</> },
            { icon: 'activity', term: 'Idle-burn watch', desc: 'Always on — flags more than ~5k tokens generated in 10 minutes while you\'re away ("what did it spend while I slept").' },
          ]}
        />
      </Panel>
    </>
  );
}
