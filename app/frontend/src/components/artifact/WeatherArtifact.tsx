import type { WeatherArtifactData, WeatherDay } from '../../lib/types';

// Inline SVG line chart: high (amber) + low (blue) with area fill + precip bars.
// Pure function of the daily data — no external chart lib.
function WeekChart({ days }: { days: WeatherDay[] }) {
  const W = 520;
  const H = 200;
  const padL = 30;
  const padR = 14;
  const padT = 18;
  const padB = 40;
  const n = days.length;
  const allT = days.flatMap((d) => [d.tmax, d.tmin]).filter((v): v is number => v != null);
  if (!allT.length) return null;

  let mn = Math.min(...allT);
  let mx = Math.max(...allT);
  const span = Math.max(mx - mn, 6);
  mn -= span * 0.15;
  mx += span * 0.18;

  const x = (i: number) => padL + (n === 1 ? (W - padL - padR) / 2 : (i * (W - padL - padR)) / (n - 1));
  const y = (t: number) => padT + ((mx - t) * (H - padT - padB)) / (mx - mn);
  const line = (key: 'tmax' | 'tmin') =>
    days.map((d, i) => `${x(i)},${y(d[key] as number)}`).join(' ');

  const bw = Math.min(26, ((W - padL - padR) / n) * 0.5);

  const gridlines = [0, 1, 2].map((g) => {
    const tv = mn + ((mx - mn) * g) / 2;
    const gy = y(tv);
    return (
      <g key={`g${g}`}>
        <line x1={padL} y1={gy} x2={W - padR} y2={gy} stroke="#283047" strokeWidth={1} />
        <text x={padL - 6} y={gy + 3} textAnchor="end" fontSize={9} fill="#7b8394">
          {Math.round(tv)}
        </text>
      </g>
    );
  });

  const areaPath =
    'M ' +
    days.map((d, i) => `${x(i)} ${y(d.tmax as number)}`).join(' L ') +
    ' L ' +
    days
      .map((d, i) => `${x(i)} ${y(d.tmin as number)}`)
      .reverse()
      .join(' L ') +
    ' Z';

  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="week temperature chart">
      {gridlines}
      {days.map((d, i) =>
        d.precip != null && d.precip > 0 ? (
          <rect
            key={`b${i}`}
            x={x(i) - bw / 2}
            y={H - padB - (d.precip / 100) * (H - padT - padB)}
            width={bw}
            height={(d.precip / 100) * (H - padT - padB)}
            rx={3}
            fill="#6ea8ff"
            opacity={0.14}
          />
        ) : null,
      )}
      <path d={areaPath} fill="#ffb454" opacity={0.06} />
      <polyline points={line('tmax')} fill="none" stroke="#ffb454" strokeWidth={2.4} strokeLinejoin="round" strokeLinecap="round" />
      <polyline points={line('tmin')} fill="none" stroke="#6ea8ff" strokeWidth={2.4} strokeLinejoin="round" strokeLinecap="round" />
      {days.map((d, i) => (
        <g key={`p${i}`}>
          {d.tmax != null && (
            <>
              <circle cx={x(i)} cy={y(d.tmax)} r={3} fill="#ffb454" />
              <text x={x(i)} y={y(d.tmax) - 7} textAnchor="middle" fontSize={9.5} fill="#ffb454">
                {d.tmax}°
              </text>
            </>
          )}
          {d.tmin != null && (
            <>
              <circle cx={x(i)} cy={y(d.tmin)} r={3} fill="#6ea8ff" />
              <text x={x(i)} y={y(d.tmin) + 15} textAnchor="middle" fontSize={9.5} fill="#6ea8ff">
                {d.tmin}°
              </text>
            </>
          )}
          <text x={x(i)} y={H - padB + 16} textAnchor="middle" fontSize={10} fill="#aab2c2">
            {d.weekday}
          </text>
        </g>
      ))}
    </svg>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="wx-stat">
      <div className="v">{v}</div>
      <div className="k">{k}</div>
    </div>
  );
}

export function WeatherArtifact({ art }: { art: WeatherArtifactData }) {
  const u = art.units?.temp || '°F';
  const c = art.current || ({} as WeatherArtifactData['current']);
  const days = art.daily || [];
  return (
    <div className="wx">
      <div className="wx-hero">
        <div className="wx-hero-main">
          <div className="wx-temp">{c.temp != null ? `${c.temp}${u}` : '—'}</div>
          <div className="wx-desc">{c.desc}</div>
          <div className="wx-loc">{art.location}</div>
        </div>
        <div className="wx-stats">
          {c.feels != null && <Stat k="Feels" v={`${c.feels}${u}`} />}
          {c.humidity != null && <Stat k="Humidity" v={`${c.humidity}%`} />}
          {c.wind != null && <Stat k="Wind" v={`${c.wind} ${art.units?.wind || 'mph'}`} />}
        </div>
      </div>

      {days.length > 0 && (
        <>
          <div className="wx-section-h">Temperature this week</div>
          <div className="wx-chart">
            <WeekChart days={days} />
          </div>

          <div className="wx-section-h">Daily forecast</div>
          <div className="wx-days">
            {days.map((d) => (
              <div className="wx-day" key={d.date}>
                <div className="d">{d.weekday}</div>
                <div className="cond">{d.desc}</div>
                <div className="rng">
                  <span className="pp">{d.precip != null ? `${d.precip}%` : ''}</span>
                  <span className="lo">{d.tmin != null ? `${d.tmin}°` : ''}</span>
                  <span className="hi">{d.tmax != null ? `${d.tmax}°` : ''}</span>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
