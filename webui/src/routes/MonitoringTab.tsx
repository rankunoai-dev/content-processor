/**
 * The project's "Monitoring" tab: run a check, read its alerts, what changed on each
 * website and how themes moved, and set how the project is watched.
 */
import { DownloadOutlined, RadarChartOutlined } from '@ant-design/icons';
import {
  Alert as AntAlert,
  App as AntApp,
  Button,
  Card,
  Empty,
  Form,
  InputNumber,
  List,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useEffect } from 'react';
import {
  monitoringApi,
  useMonitorCheck,
  useRunCheck,
  useSetWatch,
  useWatchSettings,
  type MonitorCheck,
  type SiteDiff,
  type ThemeDrift,
  type WatchSettings,
} from '../api/monitoring';
import type { Project } from '../api/projects';
import { QueryState } from '../components/QueryState';
import { MONITORING, THEMES } from '../vocabulary';

const RESCRAPE_DAYS = [0, 1, 3, 7, 14, 30];

function counts(map: Record<string, number>): string {
  return Object.entries(map)
    .map(([k, v]) => `${k} ${v}`)
    .join(', ');
}

function SiteDetail({ site }: { site: SiteDiff }) {
  return (
    <Space direction="vertical" style={{ width: '100%', minWidth: 0, overflowWrap: 'anywhere' }}>
      {site.note ? <Typography.Text type="secondary">{site.note}</Typography.Text> : null}
      {site.changed_pages.length > 0 ? (
        <List
          size="small"
          header={<Typography.Text strong>{MONITORING.changedPages}</Typography.Text>}
          dataSource={site.changed_pages}
          renderItem={(c) => (
            <List.Item>
              <Space direction="vertical" size={2} style={{ width: '100%' }}>
                <Space wrap>
                  <a href={c.url} target="_blank" rel="noreferrer noopener">
                    {c.title ?? c.url}
                  </a>
                  {c.is_pillar ? <Tag color="purple">{MONITORING.pillar}</Tag> : null}
                  {c.theme ? <Tag>{c.theme}</Tag> : null}
                  <Typography.Text type="secondary">
                    {MONITORING.columns.drift} {Math.round(c.drift * 100)}% · +{c.sections_added} −{c.sections_removed}
                  </Typography.Text>
                </Space>
                {Object.entries(c.head_changes).map(([field, v]) => (
                  <Typography.Text key={field} type="secondary">
                    {field}: “{v.before}” → “{v.after}”
                  </Typography.Text>
                ))}
                {c.prices.map((p) => (
                  <Typography.Text key={`${p.context}${p.after}`} type="danger">
                    {MONITORING.priceChange(p.before, p.after)} ({p.context})
                  </Typography.Text>
                ))}
                {c.added_text.slice(0, 2).map((t) => (
                  <Typography.Paragraph
                    key={t}
                    type="success"
                    ellipsis={{ rows: 2, tooltip: t }}
                    style={{ marginBottom: 0, maxWidth: '100%' }}
                  >
                    + {t}
                  </Typography.Paragraph>
                ))}
              </Space>
            </List.Item>
          )}
        />
      ) : null}
      {site.new_pages.length > 0 ? (
        <Typography.Paragraph style={{ marginBottom: 0 }}>
          <Typography.Text strong>{MONITORING.newPages}: </Typography.Text>
          {site.new_pages.slice(0, 15).map((p) => (
            <Tag key={p.url}>
              <a href={p.url} target="_blank" rel="noreferrer noopener">
                {p.title ?? p.url}
              </a>
            </Tag>
          ))}
          {site.new_pages.length > 15 ? `+${site.new_pages.length - 15}` : null}
        </Typography.Paragraph>
      ) : null}
      {site.removed_pages.length > 0 ? (
        <Typography.Paragraph style={{ marginBottom: 0 }}>
          <Typography.Text strong>{MONITORING.removedPages}: </Typography.Text>
          {site.removed_pages.map((p) => p.title ?? p.url).join(' · ')}
        </Typography.Paragraph>
      ) : null}
      {site.template_changes.length > 0 ? (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          {MONITORING.templateTitle}:{' '}
          {site.template_changes
            .slice(0, 8)
            .map((t) => `“${t.line}” (+${t.added_on} / −${t.removed_on})`)
            .join(' · ')}
        </Typography.Paragraph>
      ) : null}
    </Space>
  );
}

function CheckView({ check }: { check: MonitorCheck }) {
  const moved = check.theme_drift.filter(
    (t) =>
      t.status !== 'existing' ||
      Object.keys(t.new_pages).length > 0 ||
      Object.keys(t.removed_pages).length > 0 ||
      t.ownership_before !== t.ownership_after,
  );
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card size="small" title={MONITORING.alerts}>
        {check.alerts.length === 0 ? (
          <Typography.Text type="secondary">{MONITORING.noAlerts}</Typography.Text>
        ) : (
          <List
            size="small"
            dataSource={check.alerts}
            renderItem={(a) => (
              <List.Item>
                <Space direction="vertical" size={2}>
                  <Space wrap>
                    <Tag color={MONITORING.severityColor[a.severity]}>{MONITORING.severity[a.severity]}</Tag>
                    <Typography.Text strong>{a.title}</Typography.Text>
                  </Space>
                  <Typography.Text type="secondary">{a.detail}</Typography.Text>
                </Space>
              </List.Item>
            )}
          />
        )}
      </Card>
      <Table<SiteDiff>
        rowKey="website"
        size="small"
        pagination={false}
        dataSource={check.websites}
        expandable={{ expandedRowRender: (s) => <SiteDetail site={s} /> }}
        columns={[
          {
            title: MONITORING.columns.website,
            dataIndex: 'name',
            render: (name: string, s) => (s.role === 'client' ? <strong>{name}</strong> : name),
          },
          { title: MONITORING.columns.new, render: (_v, s) => s.new_pages.length },
          { title: MONITORING.columns.removed, render: (_v, s) => s.removed_pages.length },
          { title: MONITORING.columns.changed, render: (_v, s) => s.changed_pages.length },
          { title: MONITORING.columns.template, render: (_v, s) => s.template_changes.length },
          { title: MONITORING.columns.drift, render: (_v, s) => `${Math.round(s.drift * 100)}%` },
          { title: MONITORING.note, dataIndex: 'note' },
        ]}
      />
      <Card size="small" title={MONITORING.drift}>
        {moved.length === 0 ? (
          <Typography.Text type="secondary">{MONITORING.noDrift}</Typography.Text>
        ) : (
          <Table<ThemeDrift>
            rowKey="theme_id"
            size="small"
            pagination={{ pageSize: 15, hideOnSinglePage: true }}
            dataSource={moved}
            columns={[
              { title: MONITORING.driftColumns.theme, dataIndex: 'label' },
              { title: MONITORING.driftColumns.status, render: (_v, t) => MONITORING.driftStatus[t.status] },
              {
                title: MONITORING.driftColumns.ownership,
                render: (_v, t) =>
                  t.ownership_before && t.ownership_before !== t.ownership_after
                    ? `${THEMES.ownership[t.ownership_before] ?? t.ownership_before} → ${THEMES.ownership[t.ownership_after ?? ''] ?? '-'}`
                    : (THEMES.ownership[t.ownership_after ?? ''] ?? '-'),
              },
              { title: MONITORING.driftColumns.newPages, render: (_v, t) => counts(t.new_pages) },
              { title: MONITORING.driftColumns.removed, render: (_v, t) => counts(t.removed_pages) },
            ]}
          />
        )}
      </Card>
    </Space>
  );
}

function WatchCard({ project, token }: { project: Project; token: string | null }) {
  const watch = useWatchSettings(project.project_id);
  const save = useSetWatch(project.project_id);
  const [form] = Form.useForm<Pick<WatchSettings, 'auto_check' | 'rescrape_every_days' | 'push_threshold'>>();
  const { message } = AntApp.useApp();
  useEffect(() => {
    if (watch.data) form.setFieldsValue(watch.data);
  }, [watch.data, form]);
  return (
    <Card size="small" title={MONITORING.watch}>
      <QueryState query={watch} rows={3}>
        {() => (
          <Form
            form={form}
            layout="vertical"
            disabled={token === null}
            onFinish={(values) =>
              save.mutate(
                { body: values, token },
                { onSuccess: () => void message.success(MONITORING.saved), onError: (e) => void message.error(e.message) },
              )
            }
          >
            <Form.Item name="auto_check" label={MONITORING.autoCheck} extra={MONITORING.autoCheckHelp} valuePropName="checked">
              <Switch aria-label={MONITORING.autoCheck} />
            </Form.Item>
            <Form.Item name="rescrape_every_days" label={MONITORING.rescrape} extra={MONITORING.rescrapeHelp}>
              <Select
                aria-label={MONITORING.rescrape}
                style={{ maxWidth: 260 }}
                options={RESCRAPE_DAYS.map((d) => ({ value: d, label: d === 0 ? MONITORING.rescrapeOff : MONITORING.everyDays(d) }))}
              />
            </Form.Item>
            <Form.Item name="push_threshold" label={MONITORING.pushThreshold}>
              <InputNumber aria-label={MONITORING.pushThreshold} min={1} max={100} />
            </Form.Item>
            <Button htmlType="submit" loading={save.isPending}>
              {MONITORING.save}
            </Button>
          </Form>
        )}
      </QueryState>
    </Card>
  );
}

export function MonitoringTab({ project, token }: { project: Project; token: string | null }) {
  const check = useMonitorCheck(project.project_id);
  const run = useRunCheck(project.project_id);
  const { message } = AntApp.useApp();
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <AntAlert type="info" showIcon message={MONITORING.intro} />
      <QueryState query={check} rows={6}>
        {(c) => (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Space wrap>
              <Tooltip title={token === null ? MONITORING.needsUnlock : undefined}>
                <Button
                  type={c === null ? 'primary' : 'default'}
                  icon={<RadarChartOutlined />}
                  disabled={token === null}
                  loading={run.isPending}
                  onClick={() => run.mutate(token, { onError: (e) => void message.error(e.message) })}
                >
                  {run.isPending ? MONITORING.running : MONITORING.run}
                </Button>
              </Tooltip>
              {c !== null ? (
                <Button icon={<DownloadOutlined />} href={monitoringApi.workbookUrl(project.project_id)}>
                  {MONITORING.workbook}
                </Button>
              ) : null}
              {c !== null ? (
                <Typography.Text type="secondary">
                  {MONITORING.ranAt(new Date(c.created_at).toLocaleString(), c.trigger === 'auto')} ·{' '}
                  {MONITORING.stats(c.stats)}
                </Typography.Text>
              ) : null}
            </Space>
            {token === null ? <Typography.Text type="secondary">{MONITORING.needsUnlock}</Typography.Text> : null}
            {c === null ? <Empty description={MONITORING.empty} /> : <CheckView check={c} />}
          </Space>
        )}
      </QueryState>
      <WatchCard project={project} token={token} />
    </Space>
  );
}
