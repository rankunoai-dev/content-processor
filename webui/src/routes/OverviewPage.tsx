import { RightOutlined } from '@ant-design/icons';
import { Card, Col, Empty, List, Progress, Row, Space, Statistic, Tag, Typography } from 'antd';
import { Link, useParams } from 'react-router-dom';
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip as ChartTooltip } from 'recharts';
import type { Count, Overview } from '../api/types';
import { useOverview } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { HelpTip, PageHeading, TechName } from '../components/bits';
import { pagesHref } from '../state/viewParams';
import {
  BADGE_ORDER,
  OVERVIEW,
  STOP_REASONS,
  badgeInfo,
  changeLabel,
  formatDate,
  formatDuration,
  formatLabel,
  humanize,
  issueInfo,
  tierLabel,
} from '../vocabulary';

function StatCards({ run, data }: { run: string; data: Overview }) {
  const { summary } = data;
  const badges = BADGE_ORDER.filter((b) => b !== 'RUNNING' || (summary.badges[b] ?? 0) > 0);
  return (
    <div className="stat-grid">
      <Link to={pagesHref(run, {})} className="stat-link">
        <Card className="stat-card stat-card-total" size="small">
          <Statistic title={OVERVIEW.totalPages} value={summary.page_count} />
          <Typography.Text type="secondary" className="stat-foot">
            {OVERVIEW.totalPagesHelp}
          </Typography.Text>
        </Card>
      </Link>
      {badges.map((b) => {
        const info = badgeInfo(b);
        const n = summary.badges[b] ?? 0;
        const pct = summary.page_count ? Math.round((100 * n) / summary.page_count) : 0;
        return (
          <Link key={b} to={pagesHref(run, { status: [b] })} className="stat-link" aria-label={`${info.label}: ${n} pages. ${OVERVIEW.seePages}`}>
            <Card className={`stat-card${n === 0 ? ' stat-card-zero' : ''}`} size="small">
              <Statistic
                title={
                  <Space size={6}>
                    <span className="dot" style={{ background: info.color }} />
                    {info.label}
                    <TechName name={b} />
                  </Space>
                }
                value={n}
                valueStyle={n > 0 ? { color: info.color } : undefined}
              />
              <Typography.Text type="secondary" className="stat-foot">
                {OVERVIEW.pctOfPages(pct)}
              </Typography.Text>
            </Card>
          </Link>
        );
      })}
    </div>
  );
}

function StatusDonut({ data }: { data: Overview }) {
  const { summary } = data;
  const slices = BADGE_ORDER.map((b) => ({ key: b, name: badgeInfo(b).label, value: summary.badges[b] ?? 0, color: badgeInfo(b).color })).filter(
    (s) => s.value > 0,
  );
  return (
    <Card title={OVERVIEW.donutTitle} className="panel-card">
      {slices.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <div className="donut-wrap">
          <div className="donut" role="img" aria-label={slices.map((s) => `${s.name}: ${s.value}`).join(', ')}>
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={slices} dataKey="value" nameKey="name" innerRadius={62} outerRadius={96} paddingAngle={slices.length > 1 ? 2 : 0} stroke="none" isAnimationActive={false}>
                  {slices.map((s) => (
                    <Cell key={s.key} fill={s.color} />
                  ))}
                </Pie>
                <ChartTooltip formatter={(value: number, name: string) => [`${value.toLocaleString()} pages`, name]} />
              </PieChart>
            </ResponsiveContainer>
            <div className="donut-center">
              <div className="donut-number">{summary.fully_captured_pct}%</div>
              <div className="donut-label">{badgeInfo('VERIFIED_WITHIN_SCOPE').label.toLowerCase()}</div>
            </div>
          </div>
          <ul className="donut-legend">
            {slices.map((s) => (
              <li key={s.key}>
                <span className="dot" style={{ background: s.color }} />
                <span className="donut-legend-name">{s.name}</span>
                <span className="donut-legend-value">{s.value.toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

function TopReasons({ run, data }: { run: string; data: Overview }) {
  const max = Math.max(1, ...data.issues.map((i) => i.pages));
  return (
    <Card title={OVERVIEW.topReasons} className="panel-card">
      {data.issues.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={OVERVIEW.topReasonsEmpty} />
      ) : (
        <List
          itemLayout="vertical"
          dataSource={data.issues.slice(0, 8)}
          renderItem={(issue) => {
            const info = issueInfo(issue.key);
            return (
              <List.Item key={issue.key} className="reason-item">
                <Link to={pagesHref(run, { issue: [issue.key] })} className="reason-link">
                  <div className="reason-head">
                    <Typography.Text strong>{info.title}</Typography.Text>
                    <TechName name={issue.key} />
                    <span className="reason-count">
                      {OVERVIEW.pagesAffected(issue.pages)} <RightOutlined />
                    </span>
                  </div>
                  <Progress percent={(100 * issue.pages) / max} showInfo={false} size="small" strokeColor={info.level === 'error' ? '#cf1322' : info.level === 'warning' ? '#d48806' : '#1677ff'} />
                  <Typography.Text type="secondary">{info.todo}</Typography.Text>
                  {issue.retryable_pages > 0 ? (
                    <Tag bordered={false} color="green" className="reason-retry">
                      {OVERVIEW.retryable(issue.retryable_pages)}
                    </Tag>
                  ) : null}
                </Link>
              </List.Item>
            );
          }}
        />
      )}
    </Card>
  );
}

function CountList({
  title,
  help,
  counts,
  label,
  hrefFor,
}: {
  title: string;
  help?: string;
  counts: Count[];
  label: (value: string) => string;
  hrefFor?: (value: string) => string;
}) {
  const total = counts.reduce((a, c) => a + c.count, 0) || 1;
  return (
    <Card
      title={
        <Space size={6}>
          {title}
          {help ? <HelpTip text={help} /> : null}
        </Space>
      }
      className="panel-card"
      size="small"
    >
      {counts.length === 0 ? (
        <Typography.Text type="secondary">—</Typography.Text>
      ) : (
        <ul className="count-list">
          {counts.map((c) => (
            <li key={c.value}>
              <div className="count-row">
                {hrefFor ? <Link to={hrefFor(c.value)}>{label(c.value)}</Link> : <span>{label(c.value)}</span>}
                <TechName name={c.value} />
                <span className="count-value">{c.count.toLocaleString()}</span>
              </div>
              <Progress percent={(100 * c.count) / total} showInfo={false} size="small" />
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export default function OverviewPage() {
  const { runId = '' } = useParams();
  const overview = useOverview(runId);
  return (
    <QueryState query={overview} rows={10}>
      {(data) => {
        const s = data.summary;
        const from = formatDate(s.captured_from);
        const to = formatDate(s.captured_to);
        const facts = [
          from ? `${OVERVIEW.capturedOn} ${from === to ? from : `${from} – ${to}`}` : null,
          data.facts?.elapsed_s ? `${OVERVIEW.duration} ${formatDuration(data.facts.elapsed_s)}` : null,
          data.facts?.stop_reason
            ? `${OVERVIEW.outcome}: ${(STOP_REASONS[data.facts.stop_reason] ?? humanize(data.facts.stop_reason)).toLowerCase()}`
            : null,
        ].filter(Boolean);
        return (
          <>
            <PageHeading
              title={
                <Space size={10}>
                  {s.name}
                  <TechName name={s.run_id} />
                </Space>
              }
              subtitle={`${OVERVIEW.title} · ${s.hosts.join(', ')}${facts.length ? ` · ${facts.join(' · ')}` : ''}`}
            />
            <StatCards run={runId} data={data} />
            <Row gutter={[16, 16]} className="section">
              <Col xs={24} xl={10}>
                <StatusDonut data={data} />
              </Col>
              <Col xs={24} xl={14}>
                <TopReasons run={runId} data={data} />
              </Col>
            </Row>
            <Row gutter={[16, 16]} className="section">
              <Col xs={24} md={12} xl={6}>
                <CountList
                  title={OVERVIEW.contentFound}
                  help={OVERVIEW.contentFoundHelp}
                  counts={data.formats}
                  label={formatLabel}
                  hrefFor={(v) => pagesHref(runId, { format: [v] })}
                />
              </Col>
              <Col xs={24} md={12} xl={6}>
                <CountList title={OVERVIEW.sites} counts={data.hosts} label={(v) => v} hrefFor={(v) => pagesHref(runId, { host: [v] })} />
              </Col>
              <Col xs={24} md={12} xl={6}>
                <CountList title={OVERVIEW.access} counts={data.tiers} label={tierLabel} />
              </Col>
              <Col xs={24} md={12} xl={6}>
                {data.changes.length > 0 ? (
                  <CountList title={OVERVIEW.changes} counts={data.changes} label={changeLabel} />
                ) : (
                  <Card className="panel-card" size="small" title={OVERVIEW.words}>
                    <Statistic value={data.words_total} />
                  </Card>
                )}
              </Col>
            </Row>
          </>
        );
      }}
    </QueryState>
  );
}
