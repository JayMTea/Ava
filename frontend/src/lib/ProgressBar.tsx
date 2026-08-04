// Shared progress-bar primitive used everywhere work reports progress (hardware
// meters, connector-app jobs). One component => consistent look & behaviour
// across all tabs and any future modules.
//
// While `progress` is 0 it shows an indeterminate sweep (the ".prog-ind" class,
// see global.css) so a bar that's waiting on model-load / a queued task never
// looks frozen. Once progress climbs it becomes a normal determinate fill.
export function ProgressBar({
  progress,
  indeterminateAtZero = true,
  error = false,
}: {
  progress?: number;
  indeterminateAtZero?: boolean;
  error?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, Math.round(progress || 0)));
  const indet = indeterminateAtZero && pct === 0 && !error;
  return (
    <div style={{ height: 6, borderRadius: 4, background: '#2a3142', overflow: 'hidden' }}>
      <div
        className={indet ? 'prog-ind' : undefined}
        style={
          indet
            ? { height: '100%', width: '38%', background: 'var(--accent)', opacity: 0.7 }
            : { height: '100%', width: `${pct}%`, background: error ? '#e0364d' : 'var(--accent)', transition: 'width .3s' }
        }
      />
    </div>
  );
}
