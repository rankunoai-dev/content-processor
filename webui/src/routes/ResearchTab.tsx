/**
 * The project's "Research" tab: import a Semrush export (free) or pull from Semrush
 * (capped, off by default), then read rankings per website, demand per theme and movers.
 */
import { CloudDownloadOutlined, UploadOutlined } from '@ant-design/icons';
import {
  Alert as AntAlert,
  App as AntApp,
  Button,
  Card,
  Checkbox,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  Upload,
} from 'antd';
import { useMemo, useState } from 'react';
import type { Project } from '../api/projects';
import {
  useEstimate,
  useImport,
  useMovers,
  usePull,
  useResearch,
  useResearchStatus,
  type KeywordRow,
  type Mover,
  type ResearchSnapshot,
  type ThemeDemand,
  type WebsiteResearch,
} from '../api/research';
import { DownloadMenu } from '../components/DownloadMenu';
import { QueryState } from '../components/QueryState';
import { RESEARCH } from '../vocabulary';
import { SuggestedCompetitors } from './SuggestedCompetitors';

function websitesOf(project: Project) {
  return project.sites
    .filter((s) => s.domains.length > 0)
    .map((s) => ({ value: s.domains[0] as string, label: `${s.name} (${s.domains[0] as string})` }));
}

function ImportCard({ project, token }: { project: Project; token: string | null }) {
  const options = useMemo(() => websitesOf(project), [project]);
  const [website, setWebsite] = useState<string | undefined>(options[0]?.value);
  const run = useImport(project.project_id);
  const { message } = AntApp.useApp();
  return (
    <Card size="small" title={RESEARCH.importTitle}>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">{RESEARCH.importHelp}</Typography.Text>
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
            accept=".csv,text/csv"
            showUploadList={false}
            disabled={token === null || website === undefined}
            beforeUpload={(file) => {
              if (website !== undefined) {
                run.mutate(
                  { website, file, token },
                  {
                    onSuccess: (snap) => {
                      const site = snap.websites.find((w) => w.website === website);
                      void message.success(RESEARCH.imported(site?.keywords.length ?? 0));
                    },
                    onError: (e) => void message.error(e.message),
                  },
                );
              }
              return false;
            }}
          >
            <Button icon={<UploadOutlined />} loading={run.isPending} disabled={token === null || website === undefined}>
              {RESEARCH.chooseFile}
            </Button>
          </Upload>
        </Space>
      </Space>
    </Card>
  );
}

function PullCard({ project, token }: { project: Project; token: string | null }) {
  const status = useResearchStatus();
  const [text, setText] = useState('');
  const [refresh, setRefresh] = useState(false);
  const keywords = text
    .split('\n')
    .map((k) => k.trim())
    .filter(Boolean)
    .slice(0, 50);
  const on = status.data?.enabled === true;
  const estimate = useEstimate(project.project_id, keywords.length, on);
  const pull = usePull(project.project_id);
  const { message } = AntApp.useApp();
  const over = estimate.data ? estimate.data.worst_case_units > estimate.data.pull_cap : false;
  return (
    <Card size="small" title={RESEARCH.pullTitle}>
      <QueryState query={status} rows={2}>
        {(s) =>
          !s.enabled ? (
            <Typography.Text type="secondary">{RESEARCH.pullOff(s.api_key_set)}</Typography.Text>
          ) : (
            <Space direction="vertical" style={{ width: '100%' }}>
              <Typography.Text>{RESEARCH.budget(s.used_this_month, s.monthly_cap)}</Typography.Text>
              {estimate.data ? (
                <Typography.Text type={over ? 'warning' : 'secondary'}>
                  {RESEARCH.estimate(estimate.data.worst_case_units, estimate.data.websites, estimate.data.pull_cap)}
                  {over ? ` ${RESEARCH.shared}` : ''}
                </Typography.Text>
              ) : null}
              {s.min_interval_ms !== undefined && s.fresh_days !== undefined ? (
                <Typography.Text type="secondary">{RESEARCH.pacing(s.min_interval_ms, s.fresh_days)}</Typography.Text>
              ) : null}
              <Input.TextArea
                aria-label={RESEARCH.keywords}
                placeholder={RESEARCH.keywords}
                autoSize={{ minRows: 2, maxRows: 6 }}
                value={text}
                onChange={(e) => setText(e.target.value)}
                disabled={token === null}
              />
              {s.fresh_days ? (
                <Checkbox checked={refresh} onChange={(e) => setRefresh(e.target.checked)} disabled={token === null}>
                  {RESEARCH.refresh(s.fresh_days)}
                </Checkbox>
              ) : null}
              <Button
                icon={<CloudDownloadOutlined />}
                disabled={token === null}
                loading={pull.isPending}
                onClick={() =>
                  pull.mutate(
                    { keywords, token, refresh },
                    {
                      onSuccess: (snap) => void message.success(RESEARCH.pulled(snap.units_spent)),
                      onError: (e) => void message.error(e.message),
                    },
                  )
                }
              >
                {pull.isPending ? RESEARCH.pulling : RESEARCH.pull}
              </Button>
            </Space>
          )
        }
      </QueryState>
    </Card>
  );
}

function KeywordTable({ site }: { site: WebsiteResearch }) {
  return (
    <Table<KeywordRow>
      rowKey="keyword"
      size="small"
      pagination={{ pageSize: 20, hideOnSinglePage: true }}
      dataSource={site.keywords}
      columns={[
        { title: RESEARCH.columns.keyword, dataIndex: 'keyword' },
        { title: RESEARCH.columns.position, dataIndex: 'position', sorter: (a, b) => a.position - b.position },
        { title: RESEARCH.columns.previous, render: (_v, k) => k.previous_position ?? '-' },
        {
          title: RESEARCH.columns.volume,
          dataIndex: 'volume',
          sorter: (a, b) => a.volume - b.volume,
          render: (v: number) => v.toLocaleString(),
        },
        {
          title: RESEARCH.columns.url,
          render: (_v, k) =>
            k.url ? (
              <a href={k.url} target="_blank" rel="noreferrer noopener" style={{ overflowWrap: 'anywhere' }}>
                {k.url}
              </a>
            ) : (
              '-'
            ),
        },
      ]}
    />
  );
}

function counts(map: Record<string, number>): string {
  return Object.entries(map)
    .map(([k, v]) => `${k} ${v}`)
    .join(', ');
}

function SnapshotView({ snapshot, project }: { snapshot: ResearchSnapshot; project: Project }) {
  const movers = useMovers(project.project_id, true);
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {snapshot.notes.length > 0 ? (
        <AntAlert type="warning" showIcon message={snapshot.notes.join(' ')} />
      ) : null}
      <Table<WebsiteResearch>
        rowKey="website"
        size="small"
        pagination={false}
        dataSource={snapshot.websites}
        expandable={{ expandedRowRender: (s) => <KeywordTable site={s} />, rowExpandable: (s) => s.keywords.length > 0 }}
        columns={[
          {
            title: RESEARCH.columns.website,
            dataIndex: 'name',
            render: (name: string, s) => (s.role === 'client' ? <strong>{name}</strong> : name),
          },
          { title: RESEARCH.columns.source, render: (_v, s) => <Tag>{RESEARCH.source[s.source] ?? s.source}</Tag> },
          { title: RESEARCH.columns.keywords, render: (_v, s) => s.keywords.length },
          { title: RESEARCH.columns.top10, render: (_v, s) => s.keywords.filter((k) => k.position <= 10).length },
          {
            title: RESEARCH.columns.traffic,
            render: (_v, s) => (s.overview ? s.overview.organic_traffic.toLocaleString() : '-'),
          },
          { title: RESEARCH.columns.note, dataIndex: 'note' },
        ]}
      />
      <Card size="small" title={RESEARCH.demandTitle}>
        {snapshot.demand.length === 0 ? (
          <Typography.Text type="secondary">{RESEARCH.noDemand}</Typography.Text>
        ) : (
          <Table<ThemeDemand>
            rowKey={(d) => d.theme_id ?? d.label}
            size="small"
            pagination={{ pageSize: 15, hideOnSinglePage: true }}
            dataSource={snapshot.demand}
            columns={[
              { title: RESEARCH.columns.theme, dataIndex: 'label' },
              { title: RESEARCH.columns.keywords, dataIndex: 'keywords' },
              {
                title: RESEARCH.columns.demand,
                dataIndex: 'volume',
                sorter: (a, b) => a.volume - b.volume,
                render: (v: number) => v.toLocaleString(),
              },
              { title: RESEARCH.columns.ranked, render: (_v, d) => counts(d.top10) },
            ]}
          />
        )}
      </Card>
      <Card size="small" title={RESEARCH.moversTitle}>
        <QueryState query={movers} rows={2}>
          {(m) =>
            m === null ? (
              <Typography.Text type="secondary">{RESEARCH.firstTime}</Typography.Text>
            ) : m.length === 0 ? (
              <Typography.Text type="secondary">{RESEARCH.noMovers}</Typography.Text>
            ) : (
              <Table<Mover>
                rowKey={(r) => `${r.website}\u0000${r.keyword}`}
                size="small"
                pagination={{ pageSize: 15, hideOnSinglePage: true }}
                dataSource={m}
                columns={[
                  { title: RESEARCH.columns.website, dataIndex: 'website' },
                  { title: RESEARCH.columns.keyword, dataIndex: 'keyword' },
                  {
                    title: RESEARCH.columns.change,
                    render: (_v, r) =>
                      `${r.before ?? RESEARCH.notRanked} → ${r.after ?? RESEARCH.notRanked}`,
                  },
                  { title: RESEARCH.columns.volume, render: (_v, r) => r.volume.toLocaleString() },
                ]}
              />
            )
          }
        </QueryState>
      </Card>
      {snapshot.keywords.length > 0 ? (
        <Card size="small" title={RESEARCH.serpTitle}>
          <Space direction="vertical" style={{ width: '100%' }}>
            {snapshot.keywords.map((k) => (
              <Typography.Paragraph key={k.keyword} style={{ marginBottom: 0 }}>
                <Typography.Text strong>{k.keyword}</Typography.Text>{' '}
                <Typography.Text type="secondary">({k.volume.toLocaleString()})</Typography.Text>:{' '}
                {k.serp
                  .slice(0, 10)
                  .map((r) => `${r.rank}. ${r.domain}`)
                  .join(' · ')}
              </Typography.Paragraph>
            ))}
          </Space>
        </Card>
      ) : null}
    </Space>
  );
}

export function ResearchTab({ project, token }: { project: Project; token: string | null }) {
  const research = useResearch(project.project_id);
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <AntAlert type="info" showIcon message={RESEARCH.intro} />
      {token === null ? <Typography.Text type="secondary">{RESEARCH.needsUnlock}</Typography.Text> : null}
      <ImportCard project={project} token={token} />
      <PullCard project={project} token={token} />
      <SuggestedCompetitors projectId={project.project_id} token={token} />
      <QueryState query={research} rows={6}>
        {(snap) =>
          snap === null ? (
            <Empty description={RESEARCH.empty} />
          ) : (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <Space wrap>
                <DownloadMenu projectId={project.project_id} reportKey="research" />
                <Typography.Text type="secondary">
                  {RESEARCH.addedAt(new Date(snap.created_at).toLocaleString(), snap.units_spent)}
                </Typography.Text>
              </Space>
              <SnapshotView snapshot={snap} project={project} />
            </Space>
          )
        }
      </QueryState>
    </Space>
  );
}
