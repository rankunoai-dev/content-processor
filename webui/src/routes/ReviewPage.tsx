import { Card, Space, Table, Tag, Tooltip, Typography } from 'antd';
import { useParams } from 'react-router-dom';
import type { ReviewItem, ReviewResponse } from '../api/types';
import { useReview } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { PageHeading, PageLink, ReportSourceNote, StatusTag, TechName } from '../components/bits';
import { usePreferences } from '../state/preferences';
import { PAGES, REVIEW, headlineIssue, issueInfo } from '../vocabulary';

export default function ReviewPage() {
  const { runId = '' } = useParams();
  const query = useReview(runId);
  const density = usePreferences((s) => s.density);
  return (
    <>
      <PageHeading
        title={REVIEW.title}
        subtitle={REVIEW.subtitle}
        extra={query.data ? <Typography.Text type="secondary">{REVIEW.count(query.data.items.length)}</Typography.Text> : null}
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
                    width: 160,
                    render: (_, r) =>
                      r.operator_action ? (
                        <Tooltip title={r.operator_note || undefined}>
                          <Tag bordered={false} color="green">
                            {r.operator_action}
                          </Tag>
                        </Tooltip>
                      ) : (
                        <Typography.Text type="secondary">{REVIEW.noDecision}</Typography.Text>
                      ),
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
