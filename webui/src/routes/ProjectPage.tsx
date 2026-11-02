import {
  CloudDownloadOutlined,
  DeleteOutlined,
  GlobalOutlined,
  LockOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  SearchOutlined,
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
  Checkbox,
  Descriptions,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Progress,
  Segmented,
  Select,
  Space,
  Switch,
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
  useKnowledgeSearch,
  useProject,
  useProjectChange,
  useProjectRuns,
  useProjectToken,
  useProjectTokens,
  useRunReports,
  useSiteUrls,
  type ImportResult,
  type Project,
  type RoutedImport,
  type RunState,
  type Site,
} from '../api/projects';
import { QueryState } from '../components/QueryState';
import { AnalystTab } from './AnalystTab';
import { StatusBar, StatusLegend } from '../components/StatusBar';
import { PageHeading } from '../components/bits';
import { PROJECTS, formatDate, reportKind, ANALYST } from '../vocabulary';

const STATUS_COLOR: Record<string, string> = {
  queued: 'default',
  running: 'processing',
  finished: 'success',
  failed: 'error',
  cancelled: 'warning',
  interrupted: 'warning',
};

/** '30 Oct 2026, 12:31': enough to tell the scrapes of one website apart. */
function whenStarted(iso: string): string {
  const time = new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return `${formatDate(iso)}, ${time}`;
}

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function RoleTag({ site }: { site: Site }) {
  return (
    <Tag color={site.role === 'client' ? 'blue' : 'default'} bordered={false}>
      {site.role === 'client' ? PROJECTS.sites.client : PROJECTS.sites.competitor}
    </Tag>
  );
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

function RejectedList({ items }: { items: { value: string; reason: string }[] }) {
  if (items.length === 0) return null;
  return (
    <>
      <Typography.Text strong>{PROJECTS.rejectedTitle}</Typography.Text>
      <List
        size="small"
        dataSource={items}
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
  );
}

function ImportSummary({ result }: { result: ImportResult }) {
  return (
    <Alert
      type={result.added > 0 ? 'success' : 'info'}
      showIcon
      message={PROJECTS.importResult(result)}
      description={result.rejected_examples.length > 0 ? <RejectedList items={result.rejected_examples} /> : undefined}
    />
  );
}

function RoutedSummary({ result }: { result: RoutedImport }) {
  const unknown = Object.entries(result.unknown_domains);
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {result.sites.map((r) => (
        <Alert
          key={r.site_id}
          type={r.added > 0 ? 'success' : 'info'}
          showIcon
          message={`${PROJECTS.anySite.result(r)} · ${PROJECTS.importResult(r)}`}
          description={r.rejected_examples.length > 0 ? <RejectedList items={r.rejected_examples} /> : undefined}
        />
      ))}
      {unknown.map(([domain, n]) => (
        <Alert key={domain} type="warning" showIcon message={PROJECTS.anySite.unknown(domain, n)} />
      ))}
      {result.rejected.length > 0 ? (
        <Alert type="info" showIcon message={<RejectedList items={result.rejected} />} />
      ) : null}
    </Space>
  );
}

/** Paste or import addresses, either into one website or sorted to all websites. */
function AddressInput({
  unlocked,
  busy,
  onText,
  onFile,
}: {
  unlocked: boolean;
  busy: boolean;
  onText: (text: string, done: () => void) => void;
  onFile: (file: File) => void;
}) {
  const [text, setText] = useState('');
  return (
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
            loading={busy}
            onClick={() => onText(text, () => setText(''))}
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
              onFile(file);
              return false;
            }}
          >
            <Button icon={<UploadOutlined />} disabled={!unlocked} loading={busy}>
              {PROJECTS.importButton}
            </Button>
          </Upload>
        </Guarded>
        <Typography.Text type="secondary">{PROJECTS.importHelp}</Typography.Text>
      </Space>
    </Space>
  );
}

function AnySiteModal({
  project,
  token,
  open,
  onClose,
}: {
  project: Project;
  token: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const id = project.project_id;
  const { message } = AntApp.useApp();
  const [result, setResult] = useState<RoutedImport | null>(null);
  const add = useProjectChange(id, (t: string) => projectsApi.addUrlsAnySite(id, t, token));
  const importFile = useProjectChange(id, async (file: File) =>
    projectsApi.importAnySite(id, file.name, await fileToBase64(file), token),
  );
  const fail = (e: unknown) => void message.error(e instanceof Error ? e.message : String(e));
  return (
    <Modal
      title={PROJECTS.anySite.title}
      open={open}
      onCancel={() => {
        setResult(null);
        onClose();
      }}
      footer={null}
      width={720}
      destroyOnClose
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">{PROJECTS.anySite.text}</Typography.Text>
        <AddressInput
          unlocked={token !== null}
          busy={add.isPending || importFile.isPending}
          onText={(t, done) =>
            add.mutate(t, {
              onSuccess: (r) => {
                setResult(r);
                if (r.sites.some((s) => s.added > 0)) done();
              },
              onError: fail,
            })
          }
          onFile={(file) => importFile.mutate(file, { onSuccess: setResult, onError: fail })}
        />
        {result ? <RoutedSummary result={result} /> : null}
      </Space>
    </Modal>
  );
}

function AddCompetitorModal({
  project,
  token,
  open,
  onClose,
  onAdded,
}: {
  project: Project;
  token: string | null;
  open: boolean;
  onClose: () => void;
  onAdded: (site: Site) => void;
}) {
  const id = project.project_id;
  const [form] = Form.useForm<{ name: string; website?: string }>();
  const [error, setError] = useState<string | null>(null);
  const add = useProjectChange(id, (v: { name: string; website?: string }) =>
    projectsApi.addSite(id, v.name.trim(), v.website?.trim() || null, token),
  );
  return (
    <Modal
      title={PROJECTS.sites.addTitle}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={PROJECTS.sites.addCompetitor}
      confirmLoading={add.isPending}
      destroyOnClose
    >
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        onFinish={(v) =>
          add.mutate(v, {
            onSuccess: (site) => {
              setError(null);
              form.resetFields();
              onAdded(site);
              onClose();
            },
            onError: (e) => setError(e.message),
          })
        }
      >
        <Form.Item
          name="name"
          label={PROJECTS.sites.competitorName}
          rules={[{ required: true, whitespace: true, message: PROJECTS.form.required }]}
        >
          <Input maxLength={120} autoFocus />
        </Form.Item>
        <Form.Item name="website" label={PROJECTS.sites.competitorWebsite}>
          <Input maxLength={500} placeholder={PROJECTS.sites.clientWebsitePlaceholder} />
        </Form.Item>
        {error ? <Alert type="error" showIcon message={error} /> : null}
      </Form>
    </Modal>
  );
}

function UrlsPanel({ project, site, token }: { project: Project; site: Site; token: string | null }) {
  const id = project.project_id;
  const unlocked = token !== null;
  const { message } = AntApp.useApp();
  const [result, setResult] = useState<ImportResult | null>(null);
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string[]>([]);
  const size = 50;
  const urls = useSiteUrls(id, site.site_id, q, (page - 1) * size, size);
  const add = useProjectChange(id, (t: string) => projectsApi.addUrls(id, site.site_id, t, token));
  const importFile = useProjectChange(id, async (file: File) =>
    projectsApi.importFile(id, site.site_id, file.name, await fileToBase64(file), token),
  );
  const remove = useProjectChange(id, (which: string[] | 'all') =>
    projectsApi.removeUrls(id, site.site_id, which, token),
  );
  const fail = (e: unknown) => void message.error(e instanceof Error ? e.message : String(e));

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title={`${PROJECTS.addTitle}: ${site.name}`}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <AddressInput
            unlocked={unlocked}
            busy={add.isPending || importFile.isPending}
            onText={(t, done) =>
              add.mutate(t, {
                onSuccess: (r) => {
                  setResult(r);
                  if (r.added > 0) done();
                },
                onError: fail,
              })
            }
            onFile={(file) => importFile.mutate(file, { onSuccess: setResult, onError: fail })}
          />
          {result ? <ImportSummary result={result} /> : null}
        </Space>
      </Card>

      <Card
        title={PROJECTS.listTitle(site.url_count)}
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
                onClick={() => remove.mutate(selected, { onSuccess: () => setSelected([]), onError: fail })}
              >
                {PROJECTS.removeSelected(selected.length)}
              </Button>
            </Guarded>
            <Guarded unlocked={unlocked}>
              <Popconfirm
                title={PROJECTS.removeAllConfirm}
                disabled={!unlocked || site.url_count === 0}
                onConfirm={() => remove.mutate('all', { onSuccess: () => setSelected([]), onError: fail })}
              >
                <Button danger disabled={!unlocked || site.url_count === 0}>
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
            unlocked ? { selectedRowKeys: selected, onChange: (keys) => setSelected(keys.map(String)) } : undefined
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
  const cancel = useProjectChange(id, () => projectsApi.cancel(id, run.site_id, run.run_id, token));
  const pct = run.url_count ? Math.round((100 * run.pages_done) / run.url_count) : 0;
  return (
    <Card
      title={
        <Space>
          <Tag color={STATUS_COLOR[run.status]}>{PROJECTS.status[run.status]}</Tag>
          {whenStarted(run.created_at)}
        </Space>
      }
      extra={
        <Guarded unlocked={token !== null}>
          <Popconfirm
            title={PROJECTS.cancelConfirm}
            disabled={token === null}
            onConfirm={() => cancel.mutate(undefined, { onError: (e) => void message.error(e.message) })}
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
          {PROJECTS.progress(run.pages_done, run.url_count)} · {PROJECTS.batches(run.batches_done, run.batches_total)}
        </Typography.Text>
        <Typography.Text type="secondary">{run.message}</Typography.Text>
        <StatusBar badges={run.results} total={Math.max(run.url_count, 1)} />
        <StatusLegend badges={run.results} />
      </Space>
    </Card>
  );
}

function KnowledgeTag({ run }: { run: RunState }) {
  if (!run.knowledge_status) return null;
  const color = run.knowledge_status === 'ready' ? 'success' : run.knowledge_status === 'failed' ? 'warning' : 'processing';
  return (
    <Tooltip title={run.knowledge_message || undefined}>
      <Tag color={color} bordered={false}>
        {PROJECTS.knowledge[run.knowledge_status] ?? run.knowledge_status}
      </Tag>
    </Tooltip>
  );
}

function ScrapePanel({
  project,
  site,
  runs,
  token,
  onOpenReports,
}: {
  project: Project;
  site: Site;
  runs: RunState[];
  token: string | null;
  onOpenReports: (run: string) => void;
}) {
  const id = project.project_id;
  const { message } = AntApp.useApp();
  const start = useProjectChange(id, () => projectsApi.start(id, site.site_id, token));
  const active = runs.find((r) => ACTIVE.has(r.status));
  const canStart = token !== null && site.url_count > 0 && !active;
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
                {`${PROJECTS.start}: ${site.name}`}
              </Button>
            </Guarded>
            <Typography.Text type="secondary">
              {site.url_count > 0 ? PROJECTS.startHelp(site.url_count) : PROJECTS.noUrls}
            </Typography.Text>
          </Space>
        </Card>
      )}
      <Card title={PROJECTS.history}>
        <Table
          rowKey="run_id"
          size="small"
          dataSource={runs}
          pagination={false}
          locale={{ emptyText: PROJECTS.noRunYet }}
          scroll={{ x: true }}
          columns={[
            { title: PROJECTS.started, dataIndex: 'created_at', render: (d: string) => whenStarted(d) },
            { title: PROJECTS.startedBy, dataIndex: 'started_by', responsive: ['md'] },
            {
              title: PROJECTS.tabs.scrape,
              dataIndex: 'status',
              render: (s: string, r: RunState) => (
                <Space size={4} wrap>
                  <Tooltip title={s === 'finished' ? undefined : r.message}>
                    <Tag color={STATUS_COLOR[s]}>{PROJECTS.status[s]}</Tag>
                  </Tooltip>
                  <KnowledgeTag run={r} />
                </Space>
              ),
            },
            { title: PROJECTS.pages, render: (_, r: RunState) => PROJECTS.progress(r.pages_done, r.url_count) },
            {
              title: PROJECTS.results,
              width: 200,
              responsive: ['lg'],
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
      </Card>
    </Space>
  );
}

function ReportsPanel({
  project,
  site,
  runs,
  run,
  setRun,
}: {
  project: Project;
  site: Site;
  runs: RunState[];
  run: string | null;
  setRun: (r: string) => void;
}) {
  const id = project.project_id;
  const ready = runs.filter((r) => r.reports_ready);
  const chosen = run && ready.some((r) => r.run_id === run) ? run : (ready[0]?.run_id ?? null);
  const files = useRunReports(id, site.site_id, chosen);
  if (ready.length === 0) {
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
          <Button type="primary" icon={<CloudDownloadOutlined />} href={projectsApi.zipUrl(id, site.site_id, chosen)}>
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
          { title: PROJECTS.about, render: (_, f: { name: string }) => reportKind(f.name).text || f.name },
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
                <a href={projectsApi.reportUrl(id, site.site_id, chosen, f.name)} download>
                  {PROJECTS.download}
                </a>
              ) : null,
          },
        ]}
      />
    </Card>
  );
}

function SitePanel({
  project,
  site,
  runs,
  token,
  view,
  run,
  setView,
}: {
  project: Project;
  site: Site;
  runs: RunState[];
  token: string | null;
  view: string;
  run: string | null;
  setView: (view: string, run?: string) => void;
}) {
  const id = project.project_id;
  const { message } = AntApp.useApp();
  const removeSite = useProjectChange(id, () => projectsApi.removeSite(id, site.site_id, token));
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
        <Space wrap>
          <RoleTag site={site} />
          <Typography.Text type="secondary">
            <GlobalOutlined /> {site.domains.length > 0 ? site.domains.join(', ') : PROJECTS.sites.noDomain}
          </Typography.Text>
        </Space>
        <Space wrap>
          <Segmented
            value={view}
            onChange={(v) => setView(String(v))}
            options={[
              { value: 'urls', label: PROJECTS.tabs.urls },
              { value: 'scrape', label: PROJECTS.tabs.scrape },
              { value: 'reports', label: PROJECTS.tabs.reports },
            ]}
          />
          {site.role === 'competitor' ? (
            <Guarded unlocked={token !== null}>
              <Popconfirm
                title={PROJECTS.sites.removeConfirm}
                disabled={token === null}
                onConfirm={() => removeSite.mutate(undefined, { onError: (e) => void message.error(e.message) })}
              >
                <Button danger type="text" icon={<DeleteOutlined />} disabled={token === null}>
                  {PROJECTS.sites.remove}
                </Button>
              </Popconfirm>
            </Guarded>
          ) : null}
        </Space>
      </Space>
      {view === 'scrape' ? (
        <ScrapePanel
          project={project}
          site={site}
          runs={runs}
          token={token}
          onOpenReports={(r) => setView('reports', r)}
        />
      ) : view === 'reports' ? (
        <ReportsPanel project={project} site={site} runs={runs} run={run} setRun={(r) => setView('reports', r)} />
      ) : (
        <UrlsPanel project={project} site={site} token={token} />
      )}
    </Space>
  );
}

function SearchTab({ project }: { project: Project }) {
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState('');
  const clientSite = project.sites.find((s) => s.role === 'client');
  const clientDomain = clientSite?.domains[0] ?? null;
  const [everyCompetitor, setEveryCompetitor] = useState(clientDomain !== null);
  const [chosen, setChosen] = useState<string[]>(() => project.sites.map((s) => s.namespace));
  const [sideBySide, setSideBySide] = useState(false);
  const website = everyCompetitor ? clientDomain : null;
  const search = useKnowledgeSearch(query, chosen, sideBySide ? 2 : null, website);
  const names = Object.fromEntries(project.sites.map((s) => [s.site_id, s]));
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input.Search
            aria-label={PROJECTS.ask.tab}
            placeholder={PROJECTS.ask.placeholder}
            enterButton={<SearchOutlined />}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onSearch={(v) => setQuery(v.trim())}
            allowClear
          />
          <Space wrap>
            <Typography.Text strong>{PROJECTS.ask.scopeLabel}</Typography.Text>
            <Segmented
              value={everyCompetitor ? 'all' : 'project'}
              onChange={(v) => setEveryCompetitor(v === 'all')}
              options={[
                { value: 'all', label: PROJECTS.ask.everyCompetitor, disabled: clientDomain === null },
                { value: 'project', label: PROJECTS.ask.thisProject },
              ]}
            />
          </Space>
          {everyCompetitor ? (
            <Typography.Text type="secondary">{PROJECTS.ask.everyCompetitorHelp}</Typography.Text>
          ) : (
            <Space wrap>
              <Typography.Text strong>{PROJECTS.ask.websites}</Typography.Text>
              <Checkbox.Group
                value={chosen}
                onChange={(v) => setChosen(v.map(String))}
                options={project.sites.map((s) => ({ value: s.namespace, label: s.name }))}
              />
            </Space>
          )}
          {clientDomain === null ? (
            <Typography.Text type="secondary">{PROJECTS.ask.noClientDomain}</Typography.Text>
          ) : null}
          <Space>
            <Switch checked={sideBySide} onChange={setSideBySide} aria-label={PROJECTS.ask.sideBySide} />
            <Typography.Text>{PROJECTS.ask.sideBySide}</Typography.Text>
          </Space>
        </Space>
      </Card>
      {query.length >= 2 ? (
        <QueryState query={search} rows={4}>
          {(data) => (
            <Space direction="vertical" style={{ width: '100%' }}>
              {data.scope.length > 0 ? (
                <Typography.Text type="secondary">{PROJECTS.ask.considered(data.scope)}</Typography.Text>
              ) : null}
              {data.skipped.map((s) => (
                <Alert key={s.space} type="info" showIcon message={PROJECTS.ask.skipped(s.site, s.reason)} />
              ))}
              {data.hits.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={data.searched.length === 0 ? PROJECTS.ask.notReady : PROJECTS.ask.empty}
                />
              ) : (
                <List
                  dataSource={data.hits}
                  renderItem={(h) => (
                    <List.Item key={`${h.project_id}-${h.site_id}-${h.chunk_id}`}>
                      <Space direction="vertical" size={4} style={{ width: '100%' }}>
                        <Space wrap size={6}>
                          {h.project_id === project.project_id && names[h.site_id] ? (
                            <RoleTag site={names[h.site_id] as Site} />
                          ) : null}
                          <Typography.Text strong>{h.site_name}</Typography.Text>
                          {h.project_id !== project.project_id ? (
                            <Tag bordered={false}>{PROJECTS.ask.otherProject(h.project_name)}</Tag>
                          ) : null}
                          {h.headings.length > 0 ? (
                            <Typography.Text type="secondary">{h.headings.join(' › ')}</Typography.Text>
                          ) : null}
                        </Space>
                        <Typography.Paragraph style={{ marginBottom: 0 }}>{h.text}</Typography.Paragraph>
                        <a href={h.url} target="_blank" rel="noopener noreferrer">
                          {h.url}
                        </a>
                      </Space>
                    </List.Item>
                  )}
                />
              )}
            </Space>
          )}
        </QueryState>
      ) : null}
    </Space>
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
          <Descriptions.Item label={PROJECTS.sites.title}>
            {project.sites.map((s) => `${s.name} (${s.domains.join(', ') || '—'})`).join(' · ')}
          </Descriptions.Item>
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
  const runs = useProjectRuns(projectId);
  const token = useProjectToken(projectId);
  const forget = useProjectTokens((s) => s.forget);
  const { message } = AntApp.useApp();
  const [unlocking, setUnlocking] = useState(false);
  const [anySite, setAnySite] = useState(false);
  const [addingSite, setAddingSite] = useState(false);
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') ?? 'sites';
  const startAll = useProjectChange(projectId, () => projectsApi.startAll(projectId, token));
  const go = (next: Record<string, string | null>) => {
    const merged = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v === null) merged.delete(k);
      else merged.set(k, v);
    }
    setParams(merged, { replace: true });
  };

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
      {(p) => {
        const siteId = params.get('site') ?? p.sites[0]?.site_id ?? '';
        const site = p.sites.find((s) => s.site_id === siteId) ?? p.sites[0];
        const siteRuns = (runs.data ?? []).filter((r) => site && r.site_id === site.site_id);
        return (
          <>
            <Breadcrumb
              items={[{ title: <Link to="/projects">{PROJECTS.backToProjects}</Link> }, { title: p.name }]}
              style={{ marginBottom: 8 }}
            />
            <PageHeading
              title={p.name}
              subtitle={`${PROJECTS.client}: ${p.client_name} · ${PROJECTS.owner}: ${p.owner_name} · ${PROJECTS.sites.count(p.sites.length)} · ${PROJECTS.urls(p.url_count)}`}
              extra={lockControl}
            />
            <Tabs
              activeKey={tab}
              onChange={(key) => go({ tab: key })}
              items={[
                {
                  key: 'sites',
                  label: PROJECTS.sites.title,
                  children: (
                    <Space direction="vertical" size={16} style={{ width: '100%' }}>
                      <Space wrap>
                        <Guarded unlocked={token !== null}>
                          <Button icon={<UploadOutlined />} disabled={token === null} onClick={() => setAnySite(true)}>
                            {PROJECTS.anySite.button}
                          </Button>
                        </Guarded>
                        <Guarded unlocked={token !== null}>
                          <Tooltip title={PROJECTS.startAllHelp}>
                            <Button
                              icon={<PlayCircleOutlined />}
                              disabled={token === null || p.url_count === 0}
                              loading={startAll.isPending}
                              onClick={() =>
                                startAll.mutate(undefined, { onError: (e) => void message.error(e.message) })
                              }
                            >
                              {PROJECTS.startAll}
                            </Button>
                          </Tooltip>
                        </Guarded>
                      </Space>
                      <Tabs
                        type="card"
                        activeKey={site?.site_id}
                        onChange={(key) => go({ site: key, run: null })}
                        tabBarExtraContent={
                          <Guarded unlocked={token !== null}>
                            <Button
                              type="dashed"
                              icon={<PlusOutlined />}
                              disabled={token === null}
                              onClick={() => setAddingSite(true)}
                            >
                              {PROJECTS.sites.addCompetitor}
                            </Button>
                          </Guarded>
                        }
                        items={p.sites.map((s) => ({
                          key: s.site_id,
                          label: (
                            <Space size={6}>
                              {s.name}
                              {s.last_run && ACTIVE.has(s.last_run.status) ? (
                                <Tag color="processing" bordered={false}>
                                  {PROJECTS.status[s.last_run.status]}
                                </Tag>
                              ) : null}
                            </Space>
                          ),
                          children:
                            site && s.site_id === site.site_id ? (
                              <SitePanel
                                project={p}
                                site={s}
                                runs={siteRuns}
                                token={token}
                                view={params.get('view') ?? 'urls'}
                                run={params.get('run')}
                                setView={(view, run) => go({ view, run: run ?? null })}
                              />
                            ) : null,
                        }))}
                      />
                    </Space>
                  ),
                },
                { key: 'search', label: PROJECTS.ask.tab, children: <SearchTab project={p} /> },
                { key: 'analyst', label: ANALYST.tab, children: <AnalystTab project={p} token={token} /> },
                { key: 'settings', label: PROJECTS.tabs.settings, children: <SettingsTab project={p} token={token} /> },
              ]}
            />
            <UnlockModal project={p} open={unlocking} onClose={() => setUnlocking(false)} />
            <AnySiteModal project={p} token={token} open={anySite} onClose={() => setAnySite(false)} />
            <AddCompetitorModal
              project={p}
              token={token}
              open={addingSite}
              onClose={() => setAddingSite(false)}
              onAdded={(s) => go({ site: s.site_id, view: 'urls', run: null })}
            />
          </>
        );
      }}
    </QueryState>
  );
}
