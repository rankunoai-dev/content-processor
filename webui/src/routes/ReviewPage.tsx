import { Button, Card, Input, Popover, Select, Space, Table, Tooltip, Typography } from 'antd';
import { useState } from 'react';
import { useParams } from 'react-router-dom';
import type { ReviewItem, ReviewResponse } from '../api/types';
import { useReview } from '../api/queries';
import { DecidedNote, ReviewerField } from '../components/Decisions';
import { QueryState } from '../components/QueryState';
import { PageHeading, PageLink, ReportSourceNote, StatusTag, TechName } from '../components/bits';
import { usePreferences } from '../state/preferences';
import { useDecisionSaver } from '../state/useDecisionSaver';
import { PAGES, REVIEW, REVIEW_ACTIONS, SAVING, headlineIssue, issueInfo } from '../vocabulary';

function ReviewDecision({ run, item }: { run: string; item: ReviewItem }) {
  const { save, saving, canSave } = useDecisionSaver(run);
  const [draft, setDraft] = useState(item.operator_note);
  const [open, setOpen] = useState(false);
  const known = item.operator_action in REVIEW_ACTIONS;
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Tooltip title={canSave ? undefined : SAVING.needName}>
        <Select
          aria-label={REVIEW.decision}
          placeholder={REVIEW.chooseAction}
          value={item.operator_action || undefined}
          allowClear
          disabled={saving || !canSave}
          style={{ width: '100%' }}
          options={[
            ...Object.entries(REVIEW_ACTIONS).map(([value, label]) => ({ value, label })),
            // A free-text action typed into an older spreadsheet stays visible.
            ...(item.operator_action && !known ? [{ value: item.operator_action, label: item.operator_action }] : []),
          ]}
          onChange={(value?: string) => void save('review', item.url, value ?? '', item.operator_note)}
        />
      </Tooltip>
      <Popover
        trigger="click"
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (next) setDraft(item.operator_note);
        }}
        content={
          <Space direction="vertical" style={{ width: 280 }}>
            <Input.TextArea
              aria-label={REVIEW.note}
              value={draft}
              maxLength={2000}
              autoSize={{ minRows: 3, maxRows: 8 }}
              onChange={(e) => setDraft(e.target.value)}
            />
            <Button
              type="primary"
              size="small"
              disabled={!canSave || saving}
              onClick={() =>
                void save('review', item.url, item.operator_action, draft).then((ok) => ok && setOpen(false))
              }
            >
              {REVIEW.saveNote}
            </Button>
          </Space>
        }
      >
        <Button type="link" size="small" style={{ padding: 0 }}>
          {item.operator_note ? REVIEW.editNote : REVIEW.addNote}
        </Button>
      </Popover>
      {item.operator_note ? (
        <Typography.Text className="small-note" ellipsis={{ tooltip: item.operator_note }}>
          {item.operator_note}
        </Typography.Text>
      ) : null}
      <DecidedNote by={item.decided_by} at={item.decided_at} />
    </Space>
  );
}

export default function ReviewPage() {
  const { runId = '' } = useParams();
  const query = useReview(runId);
  const density = usePreferences((s) => s.density);
  return (
    <>
      <PageHeading
        title={REVIEW.title}
        subtitle={REVIEW.subtitle}
        extra={
          <Space size={16} wrap>
            {query.data ? <Typography.Text type="secondary">{REVIEW.count(query.data.items.length)}</Typography.Text> : null}
            <ReviewerField />
          </Space>
        }
      />
      <QueryState<ReviewResponse> query={query} isEmpty={(d) => d.items.length === 0} empty={REVIEW.empty}>
        {(data) => (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <ReportSourceNote source={data.source} generatedAt={data.generated_at} stale={data.stale} />
            <Card size="small" className="table-card">
              <Table<ReviewItem>
                size={density}
                rowKey="url"
                dataSource={data.items}
                tableLayout="fixed"
                scroll={{ x: 1000 }}
                pagination={data.items.length > 50 ? { pageSize: 50, showSizeChanger: false } : false}
                columns={[
                  {
                    key: 'page',
                    title: PAGES.col.page,
                    render: (_, r) => <PageLink run={runId} pageKey={r.page_key} title={r.title} url={r.url} />,
                  },
                  { key: 'status', title: PAGES.col.status, width: 180, render: (_, r) => <StatusTag badge={r.badge} /> },
                  {
                    key: 'issue',
                    title: REVIEW.mainIssue,
                    width: 260,
                    render: (_, r) => {
                      const keys = r.issue_keys.length ? r.issue_keys : r.gap_types;
                      const top = headlineIssue(keys);
                      if (!top) return <Typography.Text type="secondary">—</Typography.Text>;
                      return (
                        <Space direction="vertical" size={0}>
                          <Typography.Text strong>{issueInfo(top).title}</Typography.Text>
                          {keys.length > 1 ? (
                            <Tooltip title={keys.map((k) => issueInfo(k).title).join(' · ')}>
                              <Typography.Text type="secondary" className="small-note">
                                {REVIEW.otherIssues(keys.length - 1)}
                              </Typography.Text>
                            </Tooltip>
                          ) : null}
                          <TechName name={keys.join(', ')} />
                        </Space>
                      );
                    },
                  },
                  {
                    key: 'todo',
                    title: REVIEW.whatToDo,
                    render: (_, r) => {
                      const top = headlineIssue(r.issue_keys.length ? r.issue_keys : r.gap_types);
                      return (
                        <Space direction="vertical" size={0}>
                          <Typography.Text>{top ? issueInfo(top).todo : r.suggested_action}</Typography.Text>
                          <TechName name={r.suggested_action} />
                        </Space>
                      );
                    },
                  },
                  {
                    key: 'decision',
                    title: REVIEW.decision,
                    width: 240,
                    render: (_, r) => <ReviewDecision run={runId} item={r} />,
                  },
                ]}
              />
            </Card>
          </Space>
        )}
      </QueryState>
    </>
  );
}
