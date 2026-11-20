/**
 * A competitor website's "Lite scan" card: scrape a capped sample instead of the whole
 * website, and show what the latest lite scan covered.
 */
import { ThunderboltOutlined } from '@ant-design/icons';
import { App as AntApp, Button, Card, Space, Table, Tag, Typography } from 'antd';
import { useLiteScan, useStartLiteScan, type LiteSection } from '../api/lite';
import type { Project, Site } from '../api/projects';
import { LITE } from '../vocabulary';

export function LiteScanCard({
  project,
  site,
  token,
  busy,
}: {
  project: Project;
  site: Site;
  token: string | null;
  busy: boolean;
}) {
  const { message } = AntApp.useApp();
  const latest = useLiteScan(project.project_id, site.site_id);
  const start = useStartLiteScan(project.project_id, site.site_id, token);
  const found = latest.data ?? null;
  return (
    <Card title={LITE.title} size="small">
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          {LITE.help}
        </Typography.Paragraph>
        <Button
          icon={<ThunderboltOutlined />}
          disabled={token === null || busy}
          loading={start.isPending}
          onClick={() =>
            start.mutate(undefined, {
              onSuccess: (s) => void message.success(LITE.queued(s.sampled, s.inventory)),
              onError: (e) => void message.error(e.message),
            })
          }
        >
          {LITE.start}
        </Button>
        {token === null ? <Typography.Text type="secondary">{LITE.needsUnlock}</Typography.Text> : null}
        {found ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text>
              <Tag color="purple">{LITE.tag}</Tag>
              {LITE.latest(new Date(found.created_at).toLocaleString(), found.basis)}
            </Typography.Text>
            {found.sitemap_note ? <Typography.Text type="secondary">{LITE.sitemapNote(found.sitemap_note)}</Typography.Text> : null}
            <Table<LiteSection>
              rowKey="section"
              size="small"
              pagination={{ pageSize: 8, hideOnSinglePage: true }}
              dataSource={found.sections}
              columns={[
                { title: LITE.section, dataIndex: 'section' },
                { title: LITE.pages, dataIndex: 'pages', render: (n: number) => n.toLocaleString() },
                { title: LITE.sampled, dataIndex: 'sampled' },
              ]}
            />
          </Space>
        ) : null}
      </Space>
    </Card>
  );
}
