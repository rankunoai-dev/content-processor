/**
 * The Monitoring tab's "Recrawl plan": which pages the next pass checks and which it
 * carries forward, and why; preview it or recrawl now.
 */
import { CloudSyncOutlined, EyeOutlined } from '@ant-design/icons';
import { App as AntApp, Button, Card, Space, Table, Tag, Tooltip, Typography } from 'antd';
import { useMakePlan, useRecrawlPlan, type PageDecision, type SitePlan } from '../api/monitoring';
import type { Project } from '../api/projects';
import { DownloadMenu } from '../components/DownloadMenu';
import { QueryState } from '../components/QueryState';
import { RECRAWL } from '../vocabulary';

function Pages({ site }: { site: SitePlan }) {
  return (
    <Table<PageDecision>
      rowKey="url"
      size="small"
      scroll={{ x: true }}
      pagination={{ pageSize: 20, hideOnSinglePage: true }}
      dataSource={site.pages}
      columns={[
        {
          title: RECRAWL.columns.page,
          render: (_v, p) => (
            <a href={p.url} target="_blank" rel="noreferrer noopener" style={{ overflowWrap: 'anywhere' }}>
              {p.url}
            </a>
          ),
        },
        {
          title: RECRAWL.columns.tier,
          filters: [
            { text: RECRAWL.tier.core, value: 'core' },
            { text: RECRAWL.tier.other, value: 'other' },
          ],
          onFilter: (v, p) => p.tier === v,
          render: (_v, p) => (p.tier === 'core' ? <Tag color="purple">{RECRAWL.tier.core}</Tag> : RECRAWL.tier.other),
        },
        {
          title: RECRAWL.columns.action,
          filters: [
            { text: RECRAWL.action.check, value: 'check' },
            { text: RECRAWL.action.skip, value: 'skip' },
          ],
          onFilter: (v, p) => p.action === v,
          render: (_v, p) => <Tag color={p.action === 'check' ? 'blue' : undefined}>{RECRAWL.action[p.action]}</Tag>,
        },
        { title: RECRAWL.columns.why, dataIndex: 'reason' },
      ]}
    />
  );
}

export function RecrawlCard({ project, token }: { project: Project; token: string | null }) {
  const plan = useRecrawlPlan(project.project_id);
  const make = useMakePlan(project.project_id);
  const { message } = AntApp.useApp();
  const go = (queue: boolean) =>
    make.mutate(
      { queue, token },
      {
        onSuccess: (p) =>
          void message.success(queue ? RECRAWL.queued(p.sites.filter((s) => s.run_id).length) : RECRAWL.previewed),
        onError: (e) => void message.error(e.message),
      },
    );
  return (
    <Card size="small" title={RECRAWL.title}>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">{RECRAWL.help}</Typography.Text>
        <Space wrap>
          <Tooltip title={RECRAWL.previewHelp}>
            <Button
              icon={<EyeOutlined />}
              disabled={token === null}
              loading={make.isPending && make.variables?.queue === false}
              onClick={() => go(false)}
            >
              {RECRAWL.preview}
            </Button>
          </Tooltip>
          <Tooltip title={RECRAWL.nowHelp}>
            <Button
              icon={<CloudSyncOutlined />}
              disabled={token === null}
              loading={make.isPending && make.variables?.queue === true}
              onClick={() => go(true)}
            >
              {RECRAWL.now}
            </Button>
          </Tooltip>
          {plan.data ? <DownloadMenu projectId={project.project_id} reportKey="recrawl" /> : null}
        </Space>
        <QueryState query={plan} rows={3}>
          {(p) =>
            p === null ? (
              <Typography.Text type="secondary">{RECRAWL.empty}</Typography.Text>
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Typography.Text type="secondary">
                  {RECRAWL.made(new Date(p.created_at).toLocaleString(), p.trigger)}
                </Typography.Text>
                <Table<SitePlan>
                  rowKey="site_id"
                  size="small"
                  scroll={{ x: true }}
                  pagination={false}
                  dataSource={p.sites}
                  expandable={{ expandedRowRender: (s) => <Pages site={s} />, rowExpandable: (s) => s.pages.length > 0 }}
                  columns={[
                    {
                      title: RECRAWL.columns.website,
                      render: (_v, s) => (s.role === 'client' ? <strong>{s.name}</strong> : s.name),
                    },
                    { title: RECRAWL.columns.core, dataIndex: 'core' },
                    { title: RECRAWL.columns.check, dataIndex: 'check' },
                    { title: RECRAWL.columns.skip, dataIndex: 'skip' },
                    {
                      title: RECRAWL.columns.sitemap,
                      render: (_v, s) =>
                        RECRAWL.sitemapSummary(s.sitemap_files.length, s.sitemap_dates) || s.sitemap_note,
                    },
                    {
                      title: RECRAWL.columns.scrape,
                      render: (_v, s) => (s.run_id ? <Tag color="green">{RECRAWL.queuedRun}</Tag> : s.note),
                    },
                  ]}
                />
              </Space>
            )
          }
        </QueryState>
      </Space>
    </Card>
  );
}
