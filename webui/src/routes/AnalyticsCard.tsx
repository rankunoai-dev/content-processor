/**
 * Research tab: GA4 (what happens after the click). Import the landing page export, or set
 * the property and pull from the Data API; then totals, channels, themes, buying stages,
 * leaky pages, decays, calls to action nobody completes, AI referrals and edits.
 */
import { CloudDownloadOutlined, DownloadOutlined, UploadOutlined } from '@ant-design/icons';
import { Alert, App as AntApp, Button, Card, Input, Space, Table, Tabs, Typography, Upload } from 'antd';
import { useState } from 'react';
import {
  analyticsApi,
  useAnalytics,
  useAnalyticsSettings,
  useAnalyticsStatus,
  useImportGa4,
  usePullGa4,
  useSaveGa4Settings,
  type AnalyticsReport,
  type GroupRollup,
} from '../api/analytics';
import { QueryState } from '../components/QueryState';
import { ANALYTICS } from '../vocabulary';

const pct = (v: number) => `${(v * 100).toFixed(1)}%`;
const num = (v: number) => Math.round(v).toLocaleString();

function Url({ url }: { url: string }) {
  return (
    <a href={url} target="_blank" rel="noreferrer noopener" style={{ overflowWrap: 'anywhere' }}>
      {url}
    </a>
  );
}

function Rollup({ rows, label }: { rows: GroupRollup[]; label: string }) {
  return (
    <Table<GroupRollup>
      rowKey="key"
      size="small"
      pagination={{ pageSize: 10, hideOnSinglePage: true }}
      dataSource={rows}
      columns={[
        { title: label, dataIndex: 'label' },
        { title: ANALYTICS.columns.sessions, render: (_v, g) => num(g.sessions) },
        { title: ANALYTICS.columns.organic, render: (_v, g) => num(g.organic_sessions) },
        { title: ANALYTICS.columns.conversions, render: (_v, g) => num(g.key_events) },
        { title: ANALYTICS.columns.rate, render: (_v, g) => (g.sessions ? pct(g.key_events / g.sessions) : '-') },
        { title: ANALYTICS.columns.searches, render: (_v, g) => (g.search_volume === null ? '-' : num(g.search_volume)) },
      ]}
    />
  );
}

function Report({ report, projectId }: { report: AnalyticsReport; projectId: string }) {
  const t = report.totals;
  const C = ANALYTICS.columns;
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space wrap>
        <Button icon={<DownloadOutlined />} href={analyticsApi.workbookUrl(projectId)}>
          {ANALYTICS.workbook}
        </Button>
        <Typography.Text>
          {ANALYTICS.totals(report.period, t.sessions ?? 0, t.organic_sessions ?? 0, t.engagement_rate ?? 0, t.key_events ?? 0, t.ai_sessions ?? 0)}
        </Typography.Text>
      </Space>
      {report.notes.length > 0 ? <Alert type="info" showIcon message={report.notes.join(' ')} /> : null}
      <Tabs
        items={[
          { key: 'themes', label: `${ANALYTICS.tabs.themes} (${report.themes.length})`, children: <Rollup rows={report.themes} label={C.theme} /> },
          { key: 'funnel', label: `${ANALYTICS.tabs.funnel} (${report.funnel.length})`, children: <Rollup rows={report.funnel} label={C.stage} /> },
          {
            key: 'leaky',
            label: `${ANALYTICS.tabs.leaky} (${report.leaky.length})`,
            children: (
              <Table
                rowKey="url"
                size="small"
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                dataSource={report.leaky}
                columns={[
                  { title: C.page, render: (_v, p) => <Url url={p.url} /> },
                  { title: C.organic, render: (_v, p) => num(p.organic_sessions || p.gsc_clicks || 0) },
                  { title: C.engagement, render: (_v, p) => pct(p.engagement_rate) },
                  { title: C.why, dataIndex: 'reason' },
                ]}
              />
            ),
          },
          {
            key: 'decays',
            label: `${ANALYTICS.tabs.decays} (${report.decays.length})`,
            children: (
              <Table
                rowKey={(d) => `${d.metric}${d.url}`}
                size="small"
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                dataSource={report.decays}
                columns={[
                  { title: C.page, render: (_v, d) => <Url url={d.url} /> },
                  { title: C.metric, render: (_v, d) => ANALYTICS.metric[d.metric] ?? d.metric },
                  { title: C.change, render: (_v, d) => `${num(d.before)} → ${num(d.after)} (${pct(d.change)})` },
                ]}
              />
            ),
          },
          {
            key: 'ctas',
            label: `${ANALYTICS.tabs.ctas} (${report.cta_gaps.length})`,
            children: (
              <Table
                rowKey="url"
                size="small"
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                dataSource={report.cta_gaps}
                columns={[
                  { title: C.page, render: (_v, c) => <Url url={c.url} /> },
                  { title: C.sessions, render: (_v, c) => num(c.sessions) },
                  { title: C.offers, render: (_v, c) => c.ctas.join(', ') || ANALYTICS.forms(c.forms) },
                ]}
              />
            ),
          },
          {
            key: 'ai',
            label: `${ANALYTICS.tabs.ai} (${report.ai_referrals.length})`,
            children: (
              <Table
                rowKey="url"
                size="small"
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                dataSource={report.ai_referrals}
                columns={[
                  { title: C.page, render: (_v, a) => <Url url={a.url} /> },
                  { title: C.sessions, render: (_v, a) => num(a.sessions) },
                  { title: C.sources, render: (_v, a) => Object.entries(a.sources).map(([k, v]) => `${k} ${v}`).join(', ') },
                ]}
              />
            ),
          },
          {
            key: 'edits',
            label: `${ANALYTICS.tabs.edits} (${report.post_edit.length})`,
            children: (
              <Table
                rowKey="url"
                size="small"
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                dataSource={report.post_edit}
                columns={[
                  { title: C.page, render: (_v, e) => <Url url={e.url} /> },
                  { title: C.what, dataIndex: 'change' },
                  { title: C.sessions, render: (_v, e) => `${num(e.sessions_before)} → ${num(e.sessions_after)}` },
                  { title: C.conversions, render: (_v, e) => `${num(e.key_events_before)} → ${num(e.key_events_after)}` },
                ]}
              />
            ),
          },
        ]}
      />
    </Space>
  );
}

export function AnalyticsCard({ projectId, token }: { projectId: string; token: string | null }) {
  const report = useAnalytics(projectId);
  const status = useAnalyticsStatus();
  const settings = useAnalyticsSettings(projectId);
  const importer = useImportGa4(projectId);
  const pull = usePullGa4(projectId);
  const save = useSaveGa4Settings(projectId);
  const [property, setProperty] = useState('');
  const { message } = AntApp.useApp();
  const done = (n: number) => void message.success(ANALYTICS.saved(n));
  const fail = (e: Error) => void message.error(e.message);
  const refused = status.data?.pull_refused_because ?? null;
  return (
    <Card size="small" title={ANALYTICS.title}>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">{ANALYTICS.help}</Typography.Text>
        <Space wrap>
          <Upload
            accept=".csv,text/csv"
            showUploadList={false}
            disabled={token === null}
            beforeUpload={(file) => {
              importer.mutate({ file, token }, { onSuccess: (r) => done(r.snapshots.length), onError: fail });
              return false;
            }}
          >
            <Button icon={<UploadOutlined />} loading={importer.isPending} disabled={token === null}>
              {ANALYTICS.choose}
            </Button>
          </Upload>
          <Input
            aria-label={ANALYTICS.property}
            placeholder={settings.data?.property_id ?? ANALYTICS.property}
            style={{ width: 220 }}
            value={property}
            onChange={(e) => setProperty(e.target.value.trim())}
            disabled={token === null}
          />
          <Button
            disabled={token === null || !/^\d{4,15}$/.test(property)}
            loading={save.isPending}
            onClick={() => save.mutate({ body: { property_id: property, website: null }, token }, { onError: fail })}
          >
            {ANALYTICS.saveProperty}
          </Button>
          <Button
            icon={<CloudDownloadOutlined />}
            disabled={token === null || refused !== null}
            loading={pull.isPending}
            onClick={() => pull.mutate(token, { onSuccess: (r) => done(r.snapshots.length), onError: fail })}
          >
            {ANALYTICS.pull}
          </Button>
        </Space>
        {refused ? <Typography.Text type="secondary">{ANALYTICS.pullOff(refused)}</Typography.Text> : null}
        <QueryState query={report} rows={3}>
          {(r) => (r === null ? <Typography.Text type="secondary">{ANALYTICS.empty}</Typography.Text> : <Report report={r} projectId={projectId} />)}
        </QueryState>
      </Space>
    </Card>
  );
}
