import { Tooltip } from 'antd';
import { BADGE_ORDER, badgeInfo } from '../vocabulary';

/** A thin stacked bar of capture statuses; every segment has a text tooltip. */
export function StatusBar({ badges, total }: { badges: Record<string, number>; total: number }) {
  if (total === 0) return <div className="status-bar status-bar-empty" />;
  const parts = BADGE_ORDER.filter((b) => (badges[b] ?? 0) > 0);
  return (
    <div className="status-bar" role="img" aria-label={parts.map((b) => `${badgeInfo(b).label}: ${badges[b]}`).join(', ')}>
      {parts.map((b) => {
        const n = badges[b] ?? 0;
        const info = badgeInfo(b);
        return (
          <Tooltip key={b} title={`${info.label}: ${n.toLocaleString()}`}>
            <span className="status-bar-seg" style={{ width: `${(100 * n) / total}%`, background: info.color }} />
          </Tooltip>
        );
      })}
    </div>
  );
}

export function StatusLegend({ badges }: { badges: Record<string, number> }) {
  const parts = BADGE_ORDER.filter((b) => (badges[b] ?? 0) > 0);
  return (
    <div className="status-legend">
      {parts.map((b) => (
        <span key={b} className="status-legend-item">
          <span className="dot" style={{ background: badgeInfo(b).color }} />
          {badgeInfo(b).label} <strong>{(badges[b] ?? 0).toLocaleString()}</strong>
        </span>
      ))}
    </div>
  );
}
