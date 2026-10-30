import {
  CloudDownloadOutlined,
  DeleteOutlined,
  LockOutlined,
  PlayCircleOutlined,
  StopOutlined,
  UnlockOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import {
  Alert,
  App as AntApp,
  Breadcrumb,
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd';
import { useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import {
  ACTIVE,
  fileToBase64,
  projectsApi,
  useProject,
  useProjectChange,
  useProjectRuns,
  useProjectToken,
  useProjectTokens,
  useProjectUrls,
  useRunReports,
  type ImportResult,
  type Project,
  type RunState,
} from '../api/projects';
import { QueryState } from '../components/QueryState';
import { StatusBar, StatusLegend } from '../components/StatusBar';
import { PageHeading } from '../components/bits';
import { PROJECTS, formatDate, reportKind } from '../vocabulary';

const STATUS_COLOR: Record<string, string> = {
  queued: 'default',
  running: 'processing',
  finished: 'success',
  failed: 'error',
  cancelled: 'warning',
  interrupted: 'warning',
};

/** '30 Oct 2026, 12:31': enough to tell the scrapes of one project apart. */
function whenStarted(iso: string): string {
  const time = new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return `${formatDate(iso)}, ${time}`;
}

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** A button that needs the project unlocked: disabled, with the reason, while it is not. */
function Guarded({ unlocked, children }: { unlocked: boolean; children: ReactNode }) {
  if (unlocked) return <>{children}</>;
  return (
    <Tooltip title={PROJECTS.lock.needed}>
      <span>{children}</span>
    </Tooltip>
  );
}

function UnlockModal({ project, open, onClose }: { project: Project; open: boolean; onClose: () => void }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const remember = useProjectTokens((s) => s.remember);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const done = await projectsApi.unlock(project.project_id, password);
      remember(project.project_id, done.token, done.expires_at);
      setPassword('');
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={PROJECTS.lock.title}
      open={open}
      onCancel={onClose}
      onOk={() => void submit()}
      okText={PROJECTS.lock.submit}
      okButtonProps={{ disabled: !password }}
      confirmLoading={busy}
      destroyOnClose
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">{PROJECTS.lock.text}</Typography.Text>
        <Input.Password
          aria-label={PROJECTS.lock.password}
          placeholder={PROJECTS.lock.password}
          value={password}
          autoFocus
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
          onPressEnter={() => password && void submit()}
        />
        {error ? <Alert type="error" showIcon message={error} /> : null}
      </Space>
    </Modal>
  );
}

function ImportSummary({ result }: { result: ImportResult }) {
  return (
    <Alert
      type={result.added > 0 ? 'success' : 'info'}
      showIcon
      message={PROJECTS.importResult(result)}
      description={
        result.rejected_examples.length > 0 ? (
          <>
            <Typography.Text strong>{PROJECTS.rejectedTitle}</Typography.Text>
            <List
              size="small"
              dataSource={result.rejected_examples}
              renderItem={(r) => (
                <List.Item>
                  <Typography.Text code ellipsis style={{ maxWidth: 420 }}>
                    {r.value || ' '}
                  </Typography.Text>{' '}
                  <Typography.Text type="secondary">{r.reason}</Typography.Text>
                </List.Item>
              )}
            />
          </>
        ) : undefined
      }
    />
  );
}

function UrlsTab({ project, token }: { project: Project; token: string | null }) {
  const id = project.project_id;
  const unlocked = token !== null;
  const { message } = AntApp.useApp();
  const [text, setText] = useState('');
  const [result, setResult] = useState<ImportResult | null>(null);
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string[]>([]);
  const size = 50;
  const urls = useProjectUrls(id, q, (page - 1) * size, size);
  const add = useProjectChange(id, (t: string) => projectsApi.addUrls(id, t, token));
  const importFile = useProjectChange(id, async (file: File) =>
    projectsApi.importFile(id, file.name, await fileToBase64(file), token),
  );
  const remove = useProjectChange(id, (which: string[] | 'all') => projectsApi.removeUrls(id, which, token));

  const fail = (e: unknown) => void message.error(e instanceof Error ? e.message : String(e));

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title={PROJECTS.addTitle}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text>{PROJECTS.pasteLabel}</Typography.Text>
          <Input.TextArea
            aria-label={PROJECTS.pasteLabel}
            rows={5}
            value={text}
            disabled={!unlocked}
            placeholder={PROJECTS.pastePlaceholder}
            onChange={(e) => setText(e.target.value)}
          />
          <Space wrap>
            <Guarded unlocked={unlocked}>
              <Button
                type="primary"
                disabled={!unlocked || !text.trim()}
                loading={add.isPending}
                onClick={() =>
                  add.mutate(text, {
                    onSuccess: (r) => {
                      setResult(r);
                      if (r.added > 0) setText('');
                    },
                    onError: fail,
                  })
                }
              >
                {PROJECTS.addButton}
              </Button>
            </Guarded>
            <Guarded unlocked={unlocked}>
              <Upload
                accept=".txt,.csv,.tsv,.xlsx"
                showUploadList={false}
                disabled={!unlocked}
                beforeUpload={(file) => {
                  importFile.mutate(file, { onSuccess: setResult, onError: fail });
                  return false;
                }}
              >
                <Button icon={<UploadOutlined />} disabled={!unlocked} loading={importFile.isPending}>
                  {PROJECTS.importButton}
                </Button>
              </Upload>
            </Guarded>
            <Typography.Text type="secondary">{PROJECTS.importHelp}</Typography.Text>
          </Space>
          {result ? <ImportSummary result={result} /> : null}
        </Space>
      </Card>

      <Card
        title={PROJECTS.listTitle(project.url_count)}
        extra={
          <Space wrap>
            <Input.Search
              allowClear
              aria-label={PROJECTS.search}
              placeholder={PROJECTS.search}
              onSearch={(v) => {
                setQ(v);
                setPage(1);
              }}
              style={{ width: 240 }}
            />
            <Guarded unlocked={unlocked}>
              <Button
                icon={<DeleteOutlined />}
                disabled={!unlocked || selected.length === 0}
                loading={remove.isPending}
                onClick={() =>
                  remove.mutate(selected, { onSuccess: () => setSelected([]), onError: fail })
                }
              >
                {PROJECTS.removeSelected(selected.length)}
              </Button>
            </Guarded>
            <Guarded unlocked={unlocked}>
              <Popconfirm
                title={PROJECTS.removeAllConfirm}
                disabled={!unlocked || project.url_count === 0}
                onConfirm={() => remove.mutate('all', { onSuccess: () => setSelected([]), onError: fail })}
              >
                <Button danger disabled={!unlocked || project.url_count === 0}>
                  {PROJECTS.removeAll}
                </Button>
              </Popconfirm>
            </Guarded>
          </Space>
        }
      >
        <Table
          rowKey="url"
          size="small"
          loading={urls.isFetching}
          dataSource={urls.data?.items ?? []}
          rowSelection={
            unlocked
              ? { selectedRowKeys: selected, onChange: (keys) => setSelected(keys.map(String)) }
              : undefined
          }
          pagination={{
            current: page,
            pageSize: size,
            total: urls.data?.total ?? 0,
            showSizeChanger: false,
            onChange: setPage,
          }}
          columns={[
            {
              title: 'URL',
              dataIndex: 'url',
              ellipsis: true,
              render: (url: string) => (
                <a href={url} target="_blank" rel="noopener noreferrer">
                  {url}
                </a>
              ),
            },
            { title: PROJECTS.added, dataIndex: 'added_at', width: 140, render: (d: string) => formatDate(d) },
          ]}
        />
      </Card>
    </Space>
  );
}

function ActiveRun({ project, run, token }: { project: Project; run: RunState; token: string | null }) {
  const id = project.project_id;
  const { message } = AntApp.useApp();
  const cancel = useProjectChange(id, () => projectsApi.cancel(id, run.run_id, token));
  const pct = run.url_count ? Math.round((100 * run.pages_done) / run.url_count) : 0;
  return (
    <Card
      title={
        <Space>
          <Tag color={STATUS_COLOR[run.status]}>{PROJECTS.status[run.status]}</Tag>
          {run.run_id}
        </Space>
      }
      extra={
        <Guarded unlocked={token !== null}>
          <Popconfirm
            title={PROJECTS.cancelConfirm}
            disabled={token === null}
            onConfirm={() =>
              cancel.mutate(undefined, { onError: (e) => void message.error(e.message) })
            }
          >
            <Button icon={<StopOutlined />} danger disabled={token === null} loading={cancel.isPending}>
              {PROJECTS.cancel}
            </Button>
          </Popconfirm>
        </Guarded>
      }
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Progress percent={pct} status="active" aria-label={PROJECTS.progress(run.pages_done, run.url_count)} />
        <Typography.Text>
          {PROJECTS.progress(run.pages_done, run.url_count)} ·{' '}
          {PROJECTS.batches(run.batches_done, run.batches_total)}
        </Typography.Text>
        <Typography.Text type="secondary">{run.message}</Typography.Text>
        <StatusBar badges={run.results} total={Math.max(run.url_count, 1)} />
        <StatusLegend badges={run.results} />
      </Space>
    </Card>
  );
}

function ScrapeTab({
  project,
  token,
  onOpenReports,
}: {
  project: Project;
  token: string | null;
  onOpenReports: (run: string) => void;
}) {
  const id = project.project_id;
  const { message } = AntApp.useApp();
  const runs = useProjectRuns(id);
  const start = useProjectChange(id, () => projectsApi.start(id, token));
  const active = runs.data?.find((r) => ACTIVE.has(r.status));
  const canStart = token !== null && project.url_count > 0 && !active;
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {active ? (
        <ActiveRun project={project} run={active} token={token} />
      ) : (
        <Card>
          <Space direction="vertical">
            <Guarded unlocked={token !== null}>
              <Button
                type="primary"
                size="large"
                icon={<PlayCircleOutlined />}
                disabled={!canStart}
                loading={start.isPending}
                onClick={() => start.mutate(undefined, { onError: (e) => void message.error(e.message) })}
              >
                {PROJECTS.start}
              </Button>
            </Guarded>
            <Typography.Text type="secondary">
              {project.url_count > 0 ? PROJECTS.startHelp(project.url_count) : PROJECTS.noUrls}
            </Typography.Text>
          </Space>
        </Card>
      )}
      <Card title={PROJECTS.history}>
        <QueryState query={runs} rows={3}>
          {(data) => (
            <Table
              rowKey="run_id"
              size="small"
              dataSource={data}
              pagination={false}
              locale={{ emptyText: PROJECTS.noRunYet }}
              columns={[
                { title: PROJECTS.started, dataIndex: 'created_at', render: (d: string) => whenStarted(d) },
                { title: PROJECTS.startedBy, dataIndex: 'started_by' },
                {
                  title: PROJECTS.tabs.scrape,
                  dataIndex: 'status',
                  render: (s: string, r: RunState) =>
                    s === 'finished' ? (
                      <Tag color={STATUS_COLOR[s]}>{PROJECTS.status[s]}</Tag>
                    ) : (
                      <Tooltip title={r.message}>
                        <Tag color={STATUS_COLOR[s]}>{PROJECTS.status[s]}</Tag>
                      </Tooltip>
                    ),
                },
                {
                  title: PROJECTS.pages,
                  render: (_, r: RunState) => PROJECTS.progress(r.pages_done, r.url_count),
                },
                {
                  title: PROJECTS.results,
                  width: 220,
                  render: (_, r: RunState) => <StatusBar badges={r.results} total={Math.max(r.url_count, 1)} />,
                },
                {
                  title: PROJECTS.tabs.reports,
                  render: (_, r: RunState) =>
                    r.reports_ready ? (
                      <Button type="link" onClick={() => onOpenReports(r.run_id)}>
                        {PROJECTS.download}
                      </Button>
                    ) : null,
                },
              ]}
            />
          )}
        </QueryState>
      </Card>
    </Space>
  );
}

function ReportsTab({ project, run, setRun }: { project: Project; run: string | null; setRun: (r: string) => void }) {
  const id = project.project_id;
  const runs = useProjectRuns(id);
  const ready = (runs.data ?? []).filter((r) => r.reports_ready);
  const chosen = run ?? ready[0]?.run_id ?? null;
  const files = useRunReports(id, chosen);
  if (runs.isSuccess && ready.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={PROJECTS.noReports} />;
  }
  return (
    <Card>
      <Space wrap style={{ marginBottom: 16, width: '100%', justifyContent: 'space-between' }}>
        <Space wrap>
          <Typography.Text strong>{PROJECTS.reportsFor}</Typography.Text>
          <Select
            aria-label={PROJECTS.reportsFor}
            value={chosen ?? undefined}
            style={{ width: 240, maxWidth: '100%' }}
            onChange={setRun}
            options={ready.map((r) => ({ value: r.run_id, label: whenStarted(r.created_at) }))}
          />
        </Space>
        {chosen ? (
          <Button type="primary" icon={<CloudDownloadOutlined />} href={projectsApi.zipUrl(id, chosen)}>
            {PROJECTS.downloadAll}
          </Button>
        ) : null}
      </Space>
      <Table
        rowKey="name"
        size="small"
        loading={files.isFetching}
        dataSource={[...(files.data ?? [])].sort(
          (a, b) => reportKind(a.name).rank - reportKind(b.name).rank || a.name.localeCompare(b.name),
        )}
        pagination={false}
        columns={[
          {
            title: PROJECTS.about,
            render: (_, f: { name: string }) => reportKind(f.name).text || f.name,
          },
          {
            title: PROJECTS.file,
            dataIndex: 'name',
            responsive: ['lg'],
            render: (n: string) => <Typography.Text type="secondary">{n}</Typography.Text>,
          },
          { title: PROJECTS.size, dataIndex: 'size', width: 110, render: (n: number) => bytes(n) },
          {
            title: PROJECTS.download,
            width: 130,
            render: (_, f: { name: string }) =>
              chosen ? (
                <a href={projectsApi.reportUrl(id, chosen, f.name)} download>
                  {PROJECTS.download}
                </a>
              ) : null,
          },
        ]}
      />
    </Card>
  );
}

function SettingsTab({ project, token }: { project: Project; token: string | null }) {
  const [form] = Form.useForm<{ password: string }>();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const forget = useProjectTokens((s) => s.forget);
  const navigate = useNavigate();
  const { message } = AntApp.useApp();
  const submit = async ({ password }: { password: string }) => {
    setBusy(true);
    setError(null);
    try {
      await projectsApi.remove(project.project_id, password, token);
      forget(project.project_id);
      void message.success(PROJECTS.deleted);
      navigate('/projects');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Descriptions column={1} size="small">
          <Descriptions.Item label={PROJECTS.form.name}>{project.name}</Descriptions.Item>
          <Descriptions.Item label={PROJECTS.owner}>{project.owner_name}</Descriptions.Item>
          <Descriptions.Item label={PROJECTS.client}>{project.client_name}</Descriptions.Item>
          <Descriptions.Item label={PROJECTS.created}>{formatDate(project.created_at)}</Descriptions.Item>
        </Descriptions>
      </Card>
      <Card title={PROJECTS.deleteTitle}>
        <Typography.Paragraph type="secondary">{PROJECTS.deleteText}</Typography.Paragraph>
        <Form form={form} layout="inline" onFinish={(v) => void submit(v)}>
          <Form.Item name="password" rules={[{ required: true, message: PROJECTS.form.required }]}>
            <Input.Password
              aria-label={PROJECTS.lock.password}
              placeholder={PROJECTS.lock.password}
              disabled={token === null}
              autoComplete="current-password"
            />
          </Form.Item>
          <Guarded unlocked={token !== null}>
            <Button danger htmlType="submit" loading={busy} disabled={token === null}>
              {PROJECTS.deleteButton}
            </Button>
          </Guarded>
        </Form>
        {error ? <Alert style={{ marginTop: 12 }} type="error" showIcon message={error} /> : null}
      </Card>
    </Space>
  );
}

export default function ProjectPage() {
  const { projectId = '' } = useParams();
  const project = useProject(projectId);
  const token = useProjectToken(projectId);
  const forget = useProjectTokens((s) => s.forget);
  const [unlocking, setUnlocking] = useState(false);
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') ?? 'urls';
  const run = params.get('run');
  const setTab = (key: string, extra: Record<string, string> = {}) =>
    setParams({ tab: key, ...extra }, { replace: true });

  const lockControl = useMemo(
    () =>
      token ? (
        <Space wrap>
          <Tag icon={<UnlockOutlined />} color="success">
            {PROJECTS.lock.unlocked}
          </Tag>
          <Button icon={<LockOutlined />} onClick={() => forget(projectId)}>
            {PROJECTS.lock.lockAgain}
          </Button>
        </Space>
      ) : (
        <Space wrap>
          <Tag icon={<LockOutlined />}>{PROJECTS.lock.locked}</Tag>
          <Button type="primary" icon={<UnlockOutlined />} onClick={() => setUnlocking(true)}>
            {PROJECTS.lock.unlock}
          </Button>
        </Space>
      ),
    [token, forget, projectId],
  );

  return (
    <QueryState query={project} rows={8}>
      {(p) => (
        <>
          <Breadcrumb
            items={[{ title: <Link to="/projects">{PROJECTS.backToProjects}</Link> }, { title: p.name }]}
            style={{ marginBottom: 8 }}
          />
          <PageHeading
            title={p.name}
            subtitle={`${PROJECTS.client}: ${p.client_name} · ${PROJECTS.owner}: ${p.owner_name} · ${PROJECTS.urls(p.url_count)}`}
            extra={lockControl}
          />
          <Tabs
            activeKey={tab}
            onChange={(key) => setTab(key)}
            items={[
              { key: 'urls', label: PROJECTS.tabs.urls, children: <UrlsTab project={p} token={token} /> },
              {
                key: 'scrape',
                label: PROJECTS.tabs.scrape,
                children: (
                  <ScrapeTab project={p} token={token} onOpenReports={(r) => setTab('reports', { run: r })} />
                ),
              },
              {
                key: 'reports',
                label: PROJECTS.tabs.reports,
                children: <ReportsTab project={p} run={run} setRun={(r) => setTab('reports', { run: r })} />,
              },
              { key: 'settings', label: PROJECTS.tabs.settings, children: <SettingsTab project={p} token={token} /> },
            ]}
          />
          <UnlockModal project={p} open={unlocking} onClose={() => setUnlocking(false)} />
        </>
      )}
    </QueryState>
  );
}
