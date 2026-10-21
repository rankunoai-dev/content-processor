import { Input, Space, Typography } from 'antd';
import { usePreferences } from '../state/preferences';
import { SAVING } from '../vocabulary';

/** "Saving decisions as [name]": the name recorded with every decision from this browser. */
export function ReviewerField() {
  const reviewer = usePreferences((s) => s.reviewer);
  const setReviewer = usePreferences((s) => s.setReviewer);
  return (
    <Space size={8} wrap>
      <Typography.Text type="secondary">{SAVING.reviewerLabel}</Typography.Text>
      <Input
        aria-label={SAVING.reviewerLabel}
        placeholder={SAVING.reviewerPlaceholder}
        value={reviewer}
        maxLength={80}
        status={reviewer.trim() ? undefined : 'warning'}
        onChange={(e) => setReviewer(e.target.value)}
        style={{ width: 200 }}
      />
    </Space>
  );
}

/** "Saved by Ana, 21 Oct 2026, 14:05" under a decision, when one was saved here. */
export function DecidedNote({ by, at }: { by?: string | null; at?: string | null }) {
  if (!by || !at) return null;
  const when = new Date(at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  return (
    <Typography.Text type="secondary" className="small-note">
      {SAVING.by(by, when)}
    </Typography.Text>
  );
}
