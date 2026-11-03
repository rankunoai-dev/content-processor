/**
 * The project's "Project reports" tab: one duplication and contradiction report for
 * all of the client's websites (main website and brands), built from their latest
 * finished scrapes; competitors only when asked, in their own workbook.
 */
import { CloudDownloadOutlined, FileSearchOutlined } from '@ant-design/icons';
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  Empty,
  List,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  projectKeys,
  projectsApi,
  useProjectReports,
  type Project,
  type ProjectReport,
  type ReportFile,
  type ReportSource,
} from '../api/projects';
import { QueryState } from '../components/QueryState';
import { PROJECTS, PROJECT_REPORTS, formatDate, projectReportKind } from '../vocabulary';

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const FILE_ORDER = ['contradictions_by_topic.xlsx', 'duplication.xlsx', 'competitors.xlsx', 'contradictions_by_topic.csv'];

function rank(name: string): number {
  const i = FILE_ORDER.findIndex((end) => name.endsWith(end));
  return i === -1 ? FILE_ORDER.length : i;
}

function roleLabel(s: ReportSource): string {
  if (s.role !== 'client') return PROJECT_REPORTS.competitor;
  return s.main ? PROJECT_REPORTS.mainSite : PROJECT_REPORTS.brand;
}

function Sources({ sources }: { sources: ReportSource[] }) {
  return (
    <List
      size="small"
      header={<Typography.Text strong>{PROJECT_REPORTS.websites}</Typography.Text>}
      dataSource={sources}
      renderItem={(s) => (
        <List.Item>
          <Space wrap size={[8, 4]}>
            <Typography.Text>{s.site_name}</Typography.Text>
            <Tag bordered={false} color={s.role === 'client' ? 'blue' : undefined}>
              {roleLabel(s)}
            </Tag>
            {s.run_id ? (
              <Typography.Text type="secondary">
                {PROJECT_REPORTS.scrapeOf(formatDate(s.finished_at))} · {PROJECT_REPORTS.pages(s.pages)}
              </Typography.Text>
            ) : (
              <Typography.Text type="warning">{s.reason || PROJECT_REPORTS.noScrape}</Typography.Text>
            )}
          </Space>
        </List.Item>
      )}
    />
  );
}

function Files({ projectId, report }: { projectId: string; report: ProjectReport }) {
  const files = [...report.files].sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
  return (
    <Table
      rowKey="name"
      size="small"
      pagination={false}
      dataSource={files}
      columns={[
        { title: PROJECT_REPORTS.about, render: (_, f: ReportFile) => projectReportKind(f.name) },
        {
          title: PROJECT_REPORTS.file,
          dataIndex: 'name',
          responsive: ['lg'],
          render: (n: string) => <Typography.Text type="secondary">{n}</Typography.Text>,
        },
        { title: PROJECT_REPORTS.size, dataIndex: 'size', width: 100, render: (n: number) => bytes(n) },
        {
          title: PROJECT_REPORTS.download,
          width: 110,
          render: (_, f: ReportFile) => (
            <a href={projectsApi.projectReportUrl(projectId, report.stamp, f.name)} download>
              {PROJECT_REPORTS.download}
            </a>
          ),
        },
      ]}
    />
  );
}

function Counts({ report }: { report: ProjectReport }) {
  const c = PROJECT_REPORTS.counts;
  return (
    <Row gutter={[16, 16]}>
      <Col xs={12} md={6}>
        <Statistic title={c.contradictions} value={report.contradictions} />
        <Typography.Text type="secondary">
          {report.contradictions_high} {c.high}
        </Typography.Text>
      </Col>
      <Col xs={12} md={6}>
        <Statistic title={c.condition} value={report.differs_by_condition} />
      </Col>
      <Col xs={12} md={6}>
        <Statistic title={c.timeBound} value={report.time_bound} />
      </Col>
      <Col xs={12} md={6}>
        <Statistic title={c.duplicates} value={report.duplicate_pairs} />
        <Typography.Text type="secondary">
          {report.duplicate_pairs_across_websites} {c.across}
        </Typography.Text>
      </Col>
    </Row>
  );
}

function ReportCard({ projectId, report, title }: { projectId: string; report: ProjectReport; title: string }) {
  return (
    <Card
      title={title}
      extra={
        <Button
          type="primary"
          icon={<CloudDownloadOutlined />}
          href={projectsApi.projectZipUrl(projectId, report.stamp)}
        >
          {PROJECT_REPORTS.downloadAll}
        </Button>
      }
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Typography.Text type="secondary">
          {PROJECT_REPORTS.builtOn(formatDate(report.built_at, true))} ·{' '}
          {report.websites.map((w) => w.site_name).join(', ')} · {PROJECT_REPORTS.pages(report.pages)}
        </Typography.Text>
        <Counts report={report} />
        <Files projectId={projectId} report={report} />
      </Space>
    </Card>
  );
}

export function ProjectReportsTab({ project, token }: { project: Project; token: string | null }) {
  const id = project.project_id;
  const reports = useProjectReports(id);
  const client = useQueryClient();
  const { message } = AntApp.useApp();
  const [withCompetitors, setWithCompetitors] = useState(false);
  const [busy, setBusy] = useState(false);

  const build = async () => {
    setBusy(true);
    try {
      const fresh = await projectsApi.buildProjectReports(id, withCompetitors, token);
      client.setQueryData(projectKeys.projectReports(id), fresh);
      void message.success(PROJECT_REPORTS.built);
    } catch (e) {
      void message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <QueryState query={reports} rows={6}>
      {(data) => {
        const clientReady = data.sources.some((s) => s.role === 'client' && s.run_id);
        const hasCompetitors = data.sources.some((s) => s.role === 'competitor');
        const [latest, ...older] = data.reports;
        const button = (
          <Button
            type="primary"
            icon={<FileSearchOutlined />}
            loading={busy}
            disabled={token === null || !clientReady}
            onClick={() => void build()}
          >
            {busy ? PROJECT_REPORTS.building : PROJECT_REPORTS.build}
          </Button>
        );
        return (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Card title={PROJECT_REPORTS.title}>
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Typography.Paragraph style={{ marginBottom: 0 }}>{PROJECT_REPORTS.intro}</Typography.Paragraph>
                <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
                  {PROJECT_REPORTS.whatsIn}
                </Typography.Paragraph>
                <Sources sources={data.sources} />
                {!clientReady ? <Alert type="info" showIcon message={PROJECT_REPORTS.needScrape} /> : null}
                <Space wrap>
                  {hasCompetitors ? (
                    <Tooltip title={PROJECT_REPORTS.competitorNote}>
                      <Checkbox checked={withCompetitors} onChange={(e) => setWithCompetitors(e.target.checked)}>
                        {PROJECT_REPORTS.includeCompetitors}
                      </Checkbox>
                    </Tooltip>
                  ) : null}
                  {token === null ? (
                    <Tooltip title={PROJECTS.lock.needed}>
                      <span>{button}</span>
                    </Tooltip>
                  ) : (
                    button
                  )}
                </Space>
              </Space>
            </Card>
            {latest ? (
              <ReportCard projectId={id} report={latest} title={PROJECT_REPORTS.latest} />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={PROJECT_REPORTS.none} />
            )}
            {older.length > 0 ? (
              <Collapse
                items={[
                  {
                    key: 'older',
                    label: `${PROJECT_REPORTS.older} (${older.length})`,
                    children: (
                      <Space direction="vertical" size={16} style={{ width: '100%' }}>
                        {older.map((r) => (
                          <ReportCard
                            key={r.stamp}
                            projectId={id}
                            report={r}
                            title={PROJECT_REPORTS.builtOn(formatDate(r.built_at, true))}
                          />
                        ))}
                      </Space>
                    ),
                  },
                ]}
              />
            ) : null}
          </Space>
        );
      }}
    </QueryState>
  );
}
