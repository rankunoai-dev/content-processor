/**
 * Research tab: the Search Console import and the page report (per-page search
 * performance, competing pages, pages stuck at 4-20, internal link suggestions,
 * Search Console CTR, unlinked and unranked pages).
 */
import { DownloadOutlined, UploadOutlined } from '@ant-design/icons';
import { Alert, App as AntApp, Button, Card, Select, Space, Table, Tabs, Tag, Typography, Upload } from 'antd';
import { useMemo, useState } from 'react';
import type { Project } from '../api/projects';
import {
  researchApi,
  useGsc,
  useImportGsc,
  usePageReport,
  type Cannibalisation,
  type CtrOpportunity,
  type LinkSuggestion,
  type PageReport,
  type PageRow,
} from '../api/research';
import { QueryState } from '../components/QueryState';
import { RESEARCH } from '../vocabulary';

const C = RESEARCH.pageColumns;

function Url({ url }: { url: string }) {
  return (
    <a href={url} target="_blank" rel="noreferrer noopener" style={{ overflowWrap: 'anywhere' }}>
      {url}
    </a>
  );
}

function pct(v: number | null): string {
  return v === null ? '-' : `${(v * 100).toFixed(1)}%`;
}

export function GscCard({ project, token }: { project: Project; token: string | null }) {
  const options = useMemo(
    () =>
      project.sites
        .filter((s) => s.role === 'client' && s.domains.length > 0)
        .map((s) => ({ value: s.domains[0] as string, label: `${s.name} (${s.domains[0] as string})` })),
    [project],
  );
  const [website, setWebsite] = useState<string | undefined>(options[0]?.value);
  const saved = useGsc(project.project_id);
  const run = useImportGsc(project.project_id);
  const { message } = AntApp.useApp();
  return (
    <Card size="small" title={RESEARCH.gscTitle}>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">{RESEARCH.gscHelp}</Typography.Text>
        {saved.data ? (
          <Typography.Text>
            {RESEARCH.gscSaved(saved.data.website, saved.data.period, saved.data.clicks, saved.data.impressions)}
          </Typography.Text>
        ) : null}
        <Space wrap>
          <Select
            aria-label={RESEARCH.website}
            style={{ minWidth: 260 }}
            value={website}
            onChange={setWebsite}
            options={options}
            disabled={token === null}
          />
          <Upload
            accept=".zip,.csv,application/zip,text/csv"
            showUploadList={false}
            disabled={token === null || website === undefined}
            beforeUpload={(file) => {
              if (website !== undefined) {
                run.mutate(
                  { website, file, token },
                  {
                    onSuccess: (s) => void message.success(RESEARCH.gscImported(s.queries, s.pages)),
                    onError: (e) => void message.error(e.message),
                  },
                );
              }
              return false;
            }}
          >
            <Button icon={<UploadOutlined />} loading={run.isPending} disabled={token === null || website === undefined}>
              {RESEARCH.gscChoose}
            </Button>
          </Upload>
        </Space>
      </Space>
    </Card>
  );
}

function PagesTable({ rows }: { rows: PageRow[] }) {
  return (
    <Table<PageRow>
      rowKey="url"
      size="small"
      scroll={{ x: 1100 }}
      pagination={{ pageSize: 15, hideOnSinglePage: true }}
      dataSource={rows}
      columns={[
        {
          title: C.page,
          render: (_v, r) => (
            <Space direction="vertical" size={0}>
              <Url url={r.url} />
              {r.top_keyword ? <Typography.Text type="secondary">{r.top_keyword}</Typography.Text> : null}
            </Space>
          ),
        },
        { title: C.keywords, dataIndex: 'keywords', sorter: (a, b) => a.keywords - b.keywords },
        {
          title: C.volume,
          dataIndex: 'volume',
          sorter: (a, b) => a.volume - b.volume,
          render: (v: number) => v.toLocaleString(),
        },
        { title: C.visits, dataIndex: 'visits', sorter: (a, b) => a.visits - b.visits },
        { title: C.position, render: (_v, r) => r.average_position ?? '-' },
        {
          title: C.links,
          dataIndex: 'inbound_body_links',
          sorter: (a, b) => a.inbound_body_links - b.inbound_body_links,
        },
        { title: C.pagerank, dataIndex: 'pagerank', sorter: (a, b) => a.pagerank - b.pagerank },
        { title: C.words, render: (_v, r) => r.word_count ?? '-' },
        {
          title: C.gsc,
          render: (_v, r) =>
            r.gsc_impressions === null ? '-' : `${r.gsc_clicks ?? 0} / ${r.gsc_impressions.toLocaleString()}`,
        },
        {
          title: C.flags,
          render: (_v, r) => (
            <Space size={[4, 4]} wrap>
              {r.flags.map((f) => (
                <Tag key={f} color={f === 'not in the scrape' ? 'default' : 'orange'}>
                  {f}
                </Tag>
              ))}
            </Space>
          ),
        },
      ]}
    />
  );
}

function ReportTabs({ report }: { report: PageReport }) {
  const byUrl = useMemo(() => new Map(report.pages.map((p) => [p.url, p])), [report]);
  const stuck = report.stuck.map((u) => byUrl.get(u)).filter((p): p is PageRow => p !== undefined);
  const ranking = report.pages.filter((p) => p.keywords > 0);
  const tab = (label: string, n: number) => `${label} (${n})`;
  return (
    <Tabs
      items={[
        {
          key: 'pages',
          label: tab(RESEARCH.pagesTabs.pages, ranking.length),
          children: <PagesTable rows={ranking.length > 0 ? ranking : report.pages} />,
        },
        {
          key: 'cannibalisation',
          label: tab(RESEARCH.pagesTabs.cannibalisation, report.cannibalisation.length),
          children: (
            <Table<Cannibalisation>
              rowKey={(c) => `${c.source}:${c.keyword}`}
              size="small"
              pagination={{ pageSize: 15, hideOnSinglePage: true }}
              dataSource={report.cannibalisation}
              columns={[
                { title: C.keyword, dataIndex: 'keyword' },
                { title: C.volume, render: (_v, c) => c.volume.toLocaleString() },
                {
                  title: C.pages,
                  render: (_v, c) => (
                    <Space direction="vertical" size={0}>
                      {c.pages.map((p) => (
                        <Url key={p} url={p} />
                      ))}
                    </Space>
                  ),
                },
                { title: C.found, render: (_v, c) => <Tag>{RESEARCH.foundBy[c.source] ?? c.source}</Tag> },
                { title: C.why, dataIndex: 'note' },
              ]}
            />
          ),
        },
        {
          key: 'stuck',
          label: tab(RESEARCH.pagesTabs.stuck, stuck.length),
          children: <PagesTable rows={stuck} />,
        },
        {
          key: 'links',
          label: tab(RESEARCH.pagesTabs.links, report.link_suggestions.length),
          children: (
            <Table<LinkSuggestion>
              rowKey={(s) => `${s.source_url}>${s.target_url}`}
              size="small"
              pagination={{ pageSize: 15, hideOnSinglePage: true }}
              dataSource={report.link_suggestions}
              columns={[
                { title: C.from, render: (_v, s) => <Url url={s.source_url} /> },
                { title: C.to, render: (_v, s) => <Url url={s.target_url} /> },
                { title: C.anchor, render: (_v, s) => <Typography.Text code>{s.anchor}</Typography.Text> },
                { title: C.why, dataIndex: 'reason' },
              ]}
            />
          ),
        },
        {
          key: 'ctr',
          label: tab(RESEARCH.pagesTabs.ctr, report.ctr_opportunities.length),
          children: (
            <Table<CtrOpportunity>
              rowKey={(o) => `${o.kind}:${o.label}`}
              size="small"
              pagination={{ pageSize: 15, hideOnSinglePage: true }}
              dataSource={report.ctr_opportunities}
              locale={{ emptyText: report.gsc_period === null ? RESEARCH.gscHelp : RESEARCH.noneHere }}
              columns={[
                {
                  title: C.page,
                  render: (_v, o) => (o.kind === 'page' ? <Url url={o.label} /> : <strong>{o.label}</strong>),
                },
                { title: C.position, dataIndex: 'position' },
                { title: C.ctr, render: (_v, o) => pct(o.ctr) },
                { title: C.usual, render: (_v, o) => pct(o.expected_ctr) },
                {
                  title: C.extra,
                  dataIndex: 'extra_clicks',
                  sorter: (a, b) => a.extra_clicks - b.extra_clicks,
                  render: (v: number) => v.toLocaleString(),
                },
              ]}
            />
          ),
        },
        {
          key: 'lists',
          label: tab(RESEARCH.pagesTabs.lists, report.orphans.length + report.zero_traffic.length),
          children: (
            <Space direction="vertical" style={{ width: '100%' }}>
              <Typography.Text strong>{`${RESEARCH.unlinkedTitle} (${report.orphans.length})`}</Typography.Text>
              <UrlList urls={report.orphans} />
              <Typography.Text strong>{`${RESEARCH.unrankedTitle} (${report.zero_traffic.length})`}</Typography.Text>
              <UrlList urls={report.zero_traffic} />
            </Space>
          ),
        },
      ]}
    />
  );
}

function UrlList({ urls }: { urls: string[] }) {
  if (urls.length === 0) return <Typography.Text type="secondary">{RESEARCH.noneHere}</Typography.Text>;
  return (
    <Table<{ url: string }>
      rowKey="url"
      size="small"
      showHeader={false}
      pagination={{ pageSize: 10, hideOnSinglePage: true }}
      dataSource={urls.map((url) => ({ url }))}
      columns={[{ render: (_v, r) => <Url url={r.url} /> }]}
    />
  );
}

export function PageReportCard({ projectId }: { projectId: string }) {
  const report = usePageReport(projectId, true);
  return (
    <Card size="small" title={RESEARCH.pagesTitle}>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">{RESEARCH.pagesHelp}</Typography.Text>
        <QueryState query={report} rows={4}>
          {(r) =>
            r === null ? (
              <Typography.Text type="secondary">{RESEARCH.pagesEmpty}</Typography.Text>
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Space wrap>
                  <Button icon={<DownloadOutlined />} href={researchApi.pagesWorkbookUrl(projectId)}>
                    {RESEARCH.pagesWorkbook}
                  </Button>
                  <Typography.Text type="secondary">
                    {RESEARCH.pagesSummary(r.pages.length, r.pages.filter((p) => p.keywords > 0).length)}
                  </Typography.Text>
                </Space>
                {r.notes.length > 0 ? <Alert type="info" showIcon message={r.notes.join(' ')} /> : null}
                <ReportTabs report={r} />
              </Space>
            )
          }
        </QueryState>
      </Space>
    </Card>
  );
}
