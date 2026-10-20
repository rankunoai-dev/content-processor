import { CalendarOutlined, GlobalOutlined, RightOutlined, WarningOutlined } from '@ant-design/icons';
import { Badge, Card, Col, Empty, Progress, Row, Space, Tag, Typography } from 'antd';
import { Link } from 'react-router-dom';
import type { RunSummary } from '../api/types';
import { useRuns } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { StatusBar, StatusLegend } from '../components/StatusBar';
import { PageHeading, TechName } from '../components/bits';
import { RUNS, formatDate } from '../vocabulary';

function dateRange(run: RunSummary): string {
  const from = formatDate(run.captured_from);
  const to = formatDate(run.captured_to);
  if (!from) return formatDate(run.file_modified_at);
  return from === to ? from : `${from} – ${to}`;
}

function RunCard({ run }: { run: RunSummary }) {
  const href = `/runs/${encodeURIComponent(run.name)}`;
  return (
    <Card
      className="run-card"
      hoverable
      title={
        <Space direction="vertical" size={0}>
          <Link to={href} className="run-card-title">
            {run.name}
          </Link>
          <TechName name={run.run_id} />
        </Space>
      }
      extra={
        run.running > 0 ? (
          <Badge status="processing" text={RUNS.running} />
        ) : (
          <Link to={href} aria-label={`${RUNS.open} ${run.name}`}>
            {RUNS.open} <RightOutlined />
          </Link>
        )
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Space size={16} wrap className="run-card-meta">
          <span>
            <CalendarOutlined /> {dateRange(run)}
          </span>
          <span>
            <GlobalOutlined /> {run.hosts.slice(0, 2).join(', ')}
            {run.hosts.length > 2 ? ` +${run.hosts.length - 2}` : ''}
          </span>
        </Space>
        <div className="run-card-numbers">
          <div>
            <div className="big-number">{run.page_count.toLocaleString()}</div>
            <Typography.Text type="secondary">{RUNS.pagesNoun(run.page_count)}</Typography.Text>
          </div>
          <Progress
            type="circle"
            size={64}
            percent={run.fully_captured_pct}
            format={(p) => `${Math.round(p ?? 0)}%`}
            strokeColor="#389e0d"
            aria-label={RUNS.fully(run.fully_captured_pct)}
          />
          <div>
            <Typography.Text strong>{RUNS.fully(run.fully_captured_pct)}</Typography.Text>
            <br />
            <Typography.Text type="secondary">{RUNS.issues(run.issue_count)}</Typography.Text>
          </div>
        </div>
        <StatusBar badges={run.badges} total={run.page_count} />
        <StatusLegend badges={run.badges} />
        <Space wrap>
          {!run.has_reports ? <Tag bordered={false}>{RUNS.noReports}</Tag> : null}
          {run.unreadable_lines > 0 ? (
            <Tag bordered={false} color="warning" icon={<WarningOutlined />}>
              {RUNS.unreadable(run.unreadable_lines)}
            </Tag>
          ) : null}
        </Space>
      </Space>
    </Card>
  );
}

export default function RunsPage() {
  const runs = useRuns();
  return (
    <>
      <PageHeading title={RUNS.title} subtitle={RUNS.subtitle} />
      <QueryState query={runs} rows={8}>
        {(data) =>
          data.runs.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                <Space direction="vertical">
                  <span className="empty-text">{RUNS.empty}</span>
                  <Typography.Text type="secondary" code>
                    {RUNS.emptyFolder(data.exports_dir)}
                  </Typography.Text>
                </Space>
              }
            />
          ) : (
            <Row gutter={[16, 16]}>
              {data.runs.map((run) => (
                <Col key={run.name} xs={24} lg={12} xxl={8}>
                  <RunCard run={run} />
                </Col>
              ))}
            </Row>
          )
        }
      </QueryState>
    </>
  );
}
