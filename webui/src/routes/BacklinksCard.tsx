/**
 * Research tab: the "Backlinks (Ahrefs)" card. Import an Ahrefs export (free) or pull
 * from Ahrefs (capped, off by default), then read each website's Domain Rating and
 * referring domains, the authority gap, the link intersect and the client's most linked
 * pages, and download the workbook.
 */
import { CloudDownloadOutlined, DownloadOutlined, UploadOutlined } from '@ant-design/icons';
import { Alert, App as AntApp, Button, Card, Empty, Select, Space, Table, Tag, Typography, Upload } from 'antd';
import { useMemo, useState } from 'react';
import {
  backlinksApi,
  useBacklinks,
  useBacklinksEstimate,
  useBacklinksStatus,
  useImportBacklinks,
  usePullBacklinks,
  type BacklinkSnapshot,
  type ExportKind,
  type IntersectDomain,
  type TopPage,
  type WebsiteBacklinks,
} from '../api/backlinks';
import type { Project } from '../api/projects';
import { QueryState } from '../components/QueryState';
import { BACKLINKS } from '../vocabulary';

const C = BACKLINKS.columns;

function Url({ url }: { url: string }) {
  return (
    <a href={url} target="_blank" rel="noreferrer noopener" style={{ overflowWrap: 'anywhere' }}>
      {url}
    </a>
  );
}

function ImportRow({ project, token }: { project: Project; token: string | null }) {
  const options = useMemo(
    () =>
      project.sites
        .filter((s) => s.domains.length > 0)
        .map((s) => ({ value: s.domains[0] as string, label: `${s.name} (${s.domains[0] as string})` })),
    [project],
  );
  const [website, setWebsite] = useState<string | undefined>(options[0]?.value);
  const [kind, setKind] = useState<ExportKind>('refdomains');
  const run = useImportBacklinks(project.project_id);
  const { message } = AntApp.useApp();
  const off = token === null || website === undefined;
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Text type="secondary">{BACKLINKS.importHelp}</Typography.Text>
      <Space wrap>
        <Select
          aria-label={BACKLINKS.website}
          style={{ minWidth: 240 }}
          value={website}
          onChange={setWebsite}
          options={options}
          disabled={token === null}
        />
        <Select<ExportKind>
          aria-label={BACKLINKS.kind}
          style={{ minWidth: 220 }}
          value={kind}
          onChange={setKind}
          options={(['refdomains', 'pages'] as const).map((k) => ({ value: k, label: BACKLINKS.kinds[k] }))}
          disabled={token === null}
        />
        <Upload
          accept=".csv,.tsv,.txt,text/csv"
          showUploadList={false}
          disabled={off}
          beforeUpload={(file) => {
            if (website !== undefined) {
              run.mutate(
                { website, kind, file, token },
                {
                  onSuccess: (snap) => {
                    const site = snap.websites.find((w) => w.website === website || website.endsWith(w.website));
                    const n = kind === 'pages' ? site?.top_pages.length : site?.refdomains.length;
                    void message.success(BACKLINKS.imported(n ?? 0, kind));
                  },
                  onError: (e) => void message.error(e.message),
                },
              );
            }
            return false;
          }}
        >
          <Button icon={<UploadOutlined />} loading={run.isPending} disabled={off}>
            {BACKLINKS.chooseFile}
          </Button>
        </Upload>
      </Space>
    </Space>
  );
}

function PullRow({ projectId, token }: { projectId: string; token: string | null }) {
  const status = useBacklinksStatus();
  const on = status.data?.enabled === true;
  const estimate = useBacklinksEstimate(projectId, on);
  const pull = usePullBacklinks(projectId);
  const { message } = AntApp.useApp();
  const over = estimate.data ? estimate.data.worst_case_units > estimate.data.pull_cap : false;
  return (
    <QueryState query={status} rows={1}>
      {(s) =>
        !s.enabled ? (
          <Typography.Text type="secondary">{BACKLINKS.pullOff(s.api_key_set)}</Typography.Text>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text>{BACKLINKS.budget(s.used_this_month, s.monthly_cap)}</Typography.Text>
            {estimate.data ? (
              <Typography.Text type={over ? 'warning' : 'secondary'}>
                {BACKLINKS.estimate(estimate.data.worst_case_units, estimate.data.websites, estimate.data.pull_cap)}
                {over ? ` ${BACKLINKS.shared}` : ''}
              </Typography.Text>
            ) : null}
            <Button
              icon={<CloudDownloadOutlined />}
              disabled={token === null}
              loading={pull.isPending}
              onClick={() =>
                pull.mutate(
                  { token },
                  {
                    onSuccess: (snap) => void message.success(BACKLINKS.pulled(snap.units_spent)),
                    onError: (e) => void message.error(e.message),
                  },
                )
              }
            >
              {pull.isPending ? BACKLINKS.pulling : BACKLINKS.pull}
            </Button>
          </Space>
        )
      }
    </QueryState>
  );
}

function SnapshotView({ snapshot, projectId }: { snapshot: BacklinkSnapshot; projectId: string }) {
  const { insights } = snapshot;
  const notes = [...snapshot.notes, ...insights.notes];
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <Button icon={<DownloadOutlined />} href={backlinksApi.workbookUrl(projectId)}>
          {BACKLINKS.workbook}
        </Button>
        <Typography.Text type="secondary">
          {BACKLINKS.addedAt(new Date(snapshot.created_at).toLocaleString(), snapshot.units_spent)}
        </Typography.Text>
      </Space>
      {notes.length > 0 ? <Alert type="warning" showIcon message={notes.join(' ')} /> : null}
      <Table<WebsiteBacklinks>
        rowKey="website"
        size="small"
        pagination={false}
        dataSource={snapshot.websites}
        columns={[
          {
            title: C.website,
            dataIndex: 'name',
            render: (name: string, w) => (w.role === 'client' ? <strong>{name}</strong> : name),
          },
          { title: C.source, render: (_v, w) => <Tag>{BACKLINKS.source[w.source] ?? w.source}</Tag> },
          { title: C.dr, render: (_v, w) => w.domain_rating ?? BACKLINKS.unknown },
          {
            title: C.refdomains,
            render: (_v, w) => (w.stats ? w.stats.live_refdomains.toLocaleString() : w.refdomains.length.toLocaleString()),
          },
          { title: C.listed, render: (_v, w) => w.refdomains.length },
          { title: C.lost, render: (_v, w) => (w.new_domains === null ? BACKLINKS.unknown : w.lost_domains.length) },
          { title: C.pages, render: (_v, w) => w.top_pages.length },
        ]}
      />
      {insights.authority.length > 0 ? (
        <Space direction="vertical" size={2}>
          <Typography.Text strong>{BACKLINKS.authorityTitle}</Typography.Text>
          {insights.authority.map((g) => (
            <Typography.Text key={g.competitor} type={g.gap > 0 ? 'warning' : 'secondary'}>
              {BACKLINKS.gap(g)}
            </Typography.Text>
          ))}
        </Space>
      ) : null}
      {insights.link_intersect.length === 0 ? (
        <Typography.Text type="secondary">{BACKLINKS.noIntersect}</Typography.Text>
      ) : (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text strong>
            {BACKLINKS.intersectTitle(insights.link_intersect.length, insights.min_competitors)}
          </Typography.Text>
          <Table<IntersectDomain>
            rowKey="domain"
            size="small"
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            dataSource={insights.link_intersect}
            columns={[
              { title: C.domain, dataIndex: 'domain' },
              {
                title: C.dr,
                sorter: (a, b) => (a.domain_rating ?? 0) - (b.domain_rating ?? 0),
                render: (_v, r) => r.domain_rating ?? BACKLINKS.unknown,
              },
              { title: C.competitors, render: (_v, r) => r.competitors.join(', ') },
              { title: C.links, dataIndex: 'links' },
            ]}
          />
        </Space>
      )}
      {insights.client_top_pages.length > 0 ? (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text strong>{BACKLINKS.topPagesTitle}</Typography.Text>
          <Table<TopPage>
            rowKey="url"
            size="small"
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            dataSource={insights.client_top_pages}
            columns={[
              { title: C.page, render: (_v, p) => <Url url={p.url} /> },
              { title: C.refdomains, dataIndex: 'referring_domains' },
              { title: C.links, dataIndex: 'links' },
            ]}
          />
        </Space>
      ) : null}
    </Space>
  );
}

export function BacklinksCard({ project, token }: { project: Project; token: string | null }) {
  const latest = useBacklinks(project.project_id);
  return (
    <Card size="small" title={BACKLINKS.title}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Text type="secondary">{BACKLINKS.intro}</Typography.Text>
        <ImportRow project={project} token={token} />
        <PullRow projectId={project.project_id} token={token} />
        <QueryState query={latest} rows={3}>
          {(snap) =>
            snap === null ? (
              <Empty description={BACKLINKS.empty} />
            ) : (
              <SnapshotView snapshot={snap} projectId={project.project_id} />
            )
          }
        </QueryState>
      </Space>
    </Card>
  );
}
