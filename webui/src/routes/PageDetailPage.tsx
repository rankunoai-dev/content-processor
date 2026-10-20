import { ArrowLeftOutlined } from '@ant-design/icons';
import { Alert, Badge, Card, Space, Statistic, Tabs, Typography } from 'antd';
import { Link, useParams } from 'react-router-dom';
import type { PageDetail } from '../api/types';
import { usePage } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { StatusTag, UrlLine } from '../components/bits';
import { usePreferences } from '../state/preferences';
import { useViewParams, type DetailTab } from '../state/viewParams';
import { APP, DETAIL, badgeInfo, formatDate, formatLabel, outcomeSentence } from '../vocabulary';
import { ContentTab } from './detail/ContentTab';
import { FactsTab } from './detail/FactsTab';
import { IssuesTab } from './detail/IssuesTab';
import { TechnicalTab } from './detail/TechnicalTab';

const ALERT_TYPE: Record<string, 'success' | 'info' | 'warning' | 'error'> = {
  VERIFIED_WITHIN_SCOPE: 'success',
  PARTIAL: 'warning',
  NEEDS_REVIEW: 'info',
  BLOCKED: 'error',
  FAILED: 'error',
  EXCLUDED: 'info',
  RUNNING: 'info',
};

function Header({ detail }: { detail: PageDetail }) {
  const row = detail.row;
  return (
    <Card className="detail-header">
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <Space size={8} wrap>
          <StatusTag badge={row.badge} />
          <Typography.Text type="secondary">{formatLabel(row.content_type)}</Typography.Text>
          <Typography.Text type="secondary">·</Typography.Text>
          <Typography.Text type="secondary">{row.host}</Typography.Text>
        </Space>
        <Typography.Title level={2} className="detail-title">
          {row.title}
        </Typography.Title>
        <UrlLine url={row.final_url ?? row.url} />
        <Alert
          type={ALERT_TYPE[row.badge] ?? 'info'}
          showIcon
          message={outcomeSentence(row, detail.blocks.length)}
          description={badgeInfo(row.badge).meaning}
        />
        <div className="detail-stats">
          <Statistic title={DETAIL.facts.captured} value={formatDate(row.captured_at)} />
          <Statistic title={DETAIL.stats.words} value={row.words} />
          <Statistic title={DETAIL.stats.pieces} value={detail.blocks.length} />
          <Statistic title={DETAIL.stats.issues} value={row.issues} />
          <Statistic title={DETAIL.facts.modified} value={formatDate(row.page_modified_at) || '—'} />
        </div>
      </Space>
    </Card>
  );
}

export default function PageDetailPage() {
  const { runId = '', pageKey = '' } = useParams();
  const page = usePage(runId, pageKey);
  const [view, setView] = useViewParams();
  const showTech = usePreferences((s) => s.showTechNames);
  const tab: DetailTab = view.tab === 'technical' && !showTech ? 'content' : view.tab;
  return (
    <>
      <Link to={`/runs/${encodeURIComponent(runId)}/pages`} className="back-link">
        <ArrowLeftOutlined /> {APP.nav.pages}
      </Link>
      <QueryState query={page} rows={12}>
        {(detail) => {
          const issueCount = detail.row.issues;
          return (
            <>
              <Header detail={detail} />
              <Card className="detail-tabs-card">
                <Tabs
                  activeKey={tab}
                  onChange={(key) => setView({ tab: key as DetailTab }, { replace: true })}
                  items={[
                    {
                      key: 'content',
                      label: DETAIL.tabs.content,
                      children: <ContentTab detail={detail} view={view} setView={setView} />,
                    },
                    {
                      key: 'issues',
                      label: (
                        <Space size={6}>
                          {DETAIL.tabs.issues}
                          <Badge
                            count={issueCount}
                            showZero
                            color={issueCount ? undefined : '#389e0d'}
                            overflowCount={999}
                            aria-label={`${issueCount} issues`}
                          />
                        </Space>
                      ),
                      children: <IssuesTab detail={detail} />,
                    },
                    {
                      key: 'facts',
                      label: DETAIL.tabs.facts,
                      children: <FactsTab detail={detail} run={runId} />,
                    },
                    ...(showTech
                      ? [
                          {
                            key: 'technical',
                            label: DETAIL.tabs.technical,
                            children: <TechnicalTab detail={detail} run={runId} />,
                          },
                        ]
                      : []),
                  ]}
                />
              </Card>
            </>
          );
        }}
      </QueryState>
    </>
  );
}
