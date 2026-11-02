/**
 * The project's "Analyst cards" tab: the card library, a form to check and run a
 * card, the project's card runs, and a viewer with live progress, the citation
 * check and the deliverable (or context pack).
 */
import { CopyOutlined, DownloadOutlined, FileSearchOutlined, PlayCircleOutlined } from '@ant-design/icons';
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from 'antd';
import { useMemo, useState } from 'react';
import {
  CARD_ACTIVE,
  useCard,
  useCardRun,
  useCardRuns,
  useCards,
  usePreflight,
  useStartCard,
  type Backend,
  type CardInputs,
  type CardParameter,
  type CardRun,
  type CardSummary,
  type Preflight,
  type RunEvent,
} from '../api/analyst';
import type { Project } from '../api/projects';
import { QueryState } from '../components/QueryState';
import { ANALYST } from '../vocabulary';

const MODULE_ORDER = ['content', 'theming', 'research', 'monitoring', 'gap', 'playground'];

const STATUS_COLOR: Record<string, string> = {
  queued: 'default',
  running: 'processing',
  finished: 'success',
  failed: 'error',
  refused: 'warning',
};

/** The website a person recognises: its shortest domain (how the engine names it). */
function websiteKey(domains: string[]): string | null {
  if (domains.length === 0) return null;
  return [...domains].sort((a, b) => a.length - b.length)[0] ?? null;
}

function websiteOptions(project: Project) {
  return project.sites
    .map((s) => ({ key: websiteKey(s.domains), site: s }))
    .filter((o): o is { key: string; site: (typeof project.sites)[number] } => o.key !== null)
    .map(({ key, site }) => ({ value: key, label: `${site.name} (${key})` }));
}

/** Inputs as the server wants them: empty fields left out, so project defaults apply. */
function cleanInputs(values: CardInputs): CardInputs {
  const out: CardInputs = {};
  for (const [name, value] of Object.entries(values)) {
    if (value === null || value === undefined) continue;
    if (Array.isArray(value) ? value.length === 0 : value.trim() === '') continue;
    out[name] = Array.isArray(value) ? value : value.trim();
  }
  return out;
}

function placeholderFor(p: CardParameter): string {
  if (p.default === 'client') return ANALYST.defaultClient;
  if (p.default === 'competitors') return ANALYST.defaultCompetitors;
  if (p.default) return p.default;
  return p.required ? '' : ANALYST.optional;
}

function ParameterField({ param, project }: { param: CardParameter; project: Project }) {
  const options = useMemo(() => websiteOptions(project), [project]);
  const help =
    param.kind === 'website' ? ANALYST.inputHelp.website : param.kind === 'url' ? ANALYST.inputHelp.url : param.description;
  const required = param.required && param.default === null;
  return (
    <Form.Item
      name={param.name}
      label={param.label}
      tooltip={help}
      rules={required ? [{ required: true, message: `${param.label} is required` }] : []}
    >
      {param.kind === 'website' ? (
        <Select
          aria-label={param.label}
          mode={param.many ? 'multiple' : undefined}
          allowClear
          placeholder={placeholderFor(param)}
          options={options}
        />
      ) : param.kind === 'choice' ? (
        <Select aria-label={param.label} allowClear options={param.choices.map((c) => ({ value: c, label: c }))} />
      ) : (
        <Input aria-label={param.label} placeholder={placeholderFor(param)} maxLength={param.kind === 'url' ? 2000 : 300} />
      )}
    </Form.Item>
  );
}

function PreflightResult({ check }: { check: Preflight }) {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {check.runnable ? (
        <Alert type="success" showIcon message={ANALYST.ready} />
      ) : (
        <Alert
          type="error"
          showIcon
          message={ANALYST.notReady}
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {check.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          }
        />
      )}
      {check.warnings.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          message={ANALYST.warnings}
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {check.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          }
        />
      ) : null}
    </Space>
  );
}

function CardDrawer({
  project,
  token,
  cardId,
  onClose,
  onStarted,
}: {
  project: Project;
  token: string | null;
  cardId: string | null;
  onClose: () => void;
  onStarted: (run: CardRun) => void;
}) {
  const card = useCard(cardId);
  const [form] = Form.useForm<CardInputs>();
  const [backend, setBackend] = useState<Backend>('pack');
  const preflight = usePreflight(project.project_id);
  const start = useStartCard(project.project_id);
  const { message } = AntApp.useApp();

  const check = () =>
    preflight.mutate(
      { card: cardId ?? '', inputs: cleanInputs(form.getFieldsValue()), backend },
      { onError: (e) => void message.error(e.message) },
    );
  const run = async () => {
    const values = await form.validateFields();
    start.mutate(
      { card: cardId ?? '', inputs: cleanInputs(values), backend, token },
      {
        onSuccess: (record) => {
          if (record.status === 'refused') void message.warning(record.message);
          else void message.success(ANALYST.started);
          onStarted(record);
        },
        onError: (e) => void message.error(e.message),
      },
    );
  };

  return (
    <Drawer
      open={cardId !== null}
      onClose={() => {
        preflight.reset();
        form.resetFields();
        onClose();
      }}
      width={640}
      destroyOnClose
      title={card.data?.title ?? ''}
    >
      <QueryState query={card} rows={6}>
        {(c) => (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Typography.Paragraph style={{ marginBottom: 0 }}>{c.summary}</Typography.Paragraph>
            <Typography.Text>
              <Typography.Text strong>{ANALYST.deliverable}: </Typography.Text>
              {c.deliverable}
            </Typography.Text>
            <Form form={form} layout="vertical" onValuesChange={() => preflight.reset()}>
              <Typography.Title level={5}>{ANALYST.inputs}</Typography.Title>
              {c.parameters
                .filter((p) => p.kind !== 'project')
                .map((p) => (
                  <ParameterField key={p.name} param={p} project={project} />
                ))}
              <Form.Item label={ANALYST.backend} extra={ANALYST.backendHelp[backend]}>
                <Radio.Group
                  value={backend}
                  onChange={(e) => {
                    setBackend(e.target.value as Backend);
                    preflight.reset();
                  }}
                >
                  <Space direction="vertical">
                    <Radio value="pack">{ANALYST.backends.pack}</Radio>
                    <Radio value="claude">{ANALYST.backends.claude}</Radio>
                  </Space>
                </Radio.Group>
              </Form.Item>
            </Form>
            {preflight.data ? <PreflightResult check={preflight.data} /> : null}
            <Space wrap>
              <Button icon={<FileSearchOutlined />} onClick={check} loading={preflight.isPending}>
                {preflight.isPending ? ANALYST.checking : ANALYST.check}
              </Button>
              <Tooltip title={token === null ? ANALYST.needsUnlock : undefined}>
                <Button
                  type="primary"
                  icon={<PlayCircleOutlined />}
                  disabled={token === null || preflight.data?.runnable === false}
                  loading={start.isPending}
                  onClick={() => void run()}
                >
                  {ANALYST.run}
                </Button>
              </Tooltip>
            </Space>
            {token === null ? <Typography.Text type="secondary">{ANALYST.needsUnlock}</Typography.Text> : null}
          </Space>
        )}
      </QueryState>
    </Drawer>
  );
}

function eventText(e: RunEvent): string | null {
  const flag = Boolean(e.error);
  switch (e.type) {
    case 'prefetch':
      return ANALYST.events.prefetch(String(e.tool ?? ''), flag);
    case 'tool':
      return ANALYST.events.tool(String(e.name ?? ''), flag);
    case 'cost':
      return ANALYST.events.cost(Number(e.usd ?? 0));
    case 'end': {
      // The run's notes are shown with its status; repeat them here only when it did not finish.
      const status = String(e.status);
      const note = status === 'failed' || status === 'refused' ? String(e.message ?? '') : '';
      return ANALYST.events.end(ANALYST.status[status] ?? status, note);
    }
    case 'error':
      return ANALYST.events.error(String(e.message ?? ''));
    default:
      return null;
  }
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function RunDrawer({
  project,
  runId,
  titles,
  onClose,
}: {
  project: Project;
  runId: string | null;
  titles: Record<string, string>;
  onClose: () => void;
}) {
  const run = useCardRun(project.project_id, runId);
  const { message } = AntApp.useApp();
  return (
    <Drawer
      open={runId !== null}
      onClose={onClose}
      width={820}
      title={run.data ? ANALYST.viewer.title(titles[run.data.record.card_id] ?? run.data.record.card_id) : ''}
    >
      <QueryState query={run} rows={8}>
        {({ record, text, events }) => {
          const isPack = record.backend === 'pack';
          const lines = events.map((e) => eventText(e)).filter((t): t is string => t !== null);
          const active = CARD_ACTIVE.has(record.status);
          return (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <Descriptions size="small" column={1} bordered>
                <Descriptions.Item label={ANALYST.columns.status}>
                  <Tag color={STATUS_COLOR[record.status]}>{ANALYST.status[record.status]}</Tag>
                  {record.message ? <Typography.Text type="secondary">{record.message}</Typography.Text> : null}
                </Descriptions.Item>
                <Descriptions.Item label={ANALYST.columns.how}>{ANALYST.backends[record.backend]}</Descriptions.Item>
                {!isPack ? (
                  <>
                    <Descriptions.Item label={ANALYST.columns.cost}>
                      {ANALYST.viewer.cost(record.cost_usd, record.cap_usd)}
                    </Descriptions.Item>
                    <Descriptions.Item label="Claude">
                      {record.model ?? '-'} · {ANALYST.viewer.usage(record.turns, record.tool_calls)}
                    </Descriptions.Item>
                  </>
                ) : null}
              </Descriptions>
              {record.verification ? (
                <Alert
                  type={record.verification.passed ? 'success' : 'warning'}
                  showIcon
                  message={record.verification.passed ? ANALYST.verified : ANALYST.needsReview}
                  description={ANALYST.viewer.check(record.verification)}
                />
              ) : null}
              <Card size="small" title={ANALYST.viewer.progress}>
                {lines.length > 0 ? (
                  <Timeline
                    pending={active ? ANALYST.status.running : false}
                    items={lines.map((t, i) => ({ key: i, children: t }))}
                  />
                ) : (
                  <Typography.Text type="secondary">{ANALYST.viewer.waiting}</Typography.Text>
                )}
              </Card>
              <Card
                size="small"
                title={isPack ? ANALYST.viewer.pack : ANALYST.viewer.deliverable}
                extra={
                  text ? (
                    <Space>
                      <Button
                        size="small"
                        icon={<CopyOutlined />}
                        onClick={() =>
                          void navigator.clipboard
                            .writeText(text)
                            .then(() => message.success(ANALYST.viewer.copied))
                        }
                      >
                        {ANALYST.viewer.copy}
                      </Button>
                      <Button
                        size="small"
                        icon={<DownloadOutlined />}
                        onClick={() => download(`${record.card_id}-${record.run_id}.md`, text)}
                      >
                        {ANALYST.viewer.download}
                      </Button>
                    </Space>
                  ) : null
                }
              >
                {text ? (
                  <pre className="analyst-text" aria-label={ANALYST.viewer.result}>
                    {text}
                  </pre>
                ) : (
                  <Typography.Text type="secondary">
                    {active ? ANALYST.viewer.waiting : ANALYST.viewer.noText}
                  </Typography.Text>
                )}
              </Card>
            </Space>
          );
        }}
      </QueryState>
    </Drawer>
  );
}

function Library({ cards, onOpen }: { cards: CardSummary[]; onOpen: (id: string) => void }) {
  return (
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      {MODULE_ORDER.filter((m) => cards.some((c) => c.module === m)).map((module) => (
        <section key={module}>
          <Typography.Title level={5}>{ANALYST.modules[module] ?? module}</Typography.Title>
          <div className="analyst-grid">
            {cards
              .filter((c) => c.module === module)
              .map((c) => (
                <Card
                  key={c.id}
                  size="small"
                  title={c.title}
                  className={c.available ? undefined : 'analyst-card-unavailable'}
                  actions={[
                    <Tooltip key="use" title={c.available ? undefined : ANALYST.needs(c.needs_modules)}>
                      <Button type="link" disabled={!c.available} onClick={() => onOpen(c.id)}>
                        {ANALYST.open}
                      </Button>
                    </Tooltip>,
                  ]}
                >
                  <Space direction="vertical" size={6}>
                    <Tag color="blue" bordered={false}>
                      {c.deliverable}
                    </Tag>
                    <Typography.Text type="secondary">{c.summary}</Typography.Text>
                    {!c.available ? <Typography.Text type="warning">{ANALYST.needs(c.needs_modules)}</Typography.Text> : null}
                  </Space>
                </Card>
              ))}
          </div>
        </section>
      ))}
    </Space>
  );
}

export function AnalystTab({ project, token }: { project: Project; token: string | null }) {
  const cards = useCards();
  const runs = useCardRuns(project.project_id);
  const [cardId, setCardId] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const titles = useMemo(
    () => Object.fromEntries((cards.data ?? []).map((c) => [c.id, c.title])),
    [cards.data],
  );

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert type="info" showIcon message={ANALYST.intro} />
      <QueryState query={cards} rows={6}>
        {(list) => <Library cards={list} onOpen={setCardId} />}
      </QueryState>
      <Card title={ANALYST.runsTitle} size="small">
        <QueryState query={runs} rows={3}>
          {(list) =>
            list.length === 0 ? (
              <Empty description={ANALYST.noRuns} />
            ) : (
              <Table<CardRun>
                rowKey="run_id"
                size="small"
                dataSource={list}
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                columns={[
                  { title: ANALYST.columns.card, dataIndex: 'card_id', render: (id: string) => titles[id] ?? id },
                  {
                    title: ANALYST.columns.how,
                    dataIndex: 'backend',
                    render: (b: Backend) => (b === 'pack' ? ANALYST.viewer.pack : 'Claude'),
                  },
                  {
                    title: ANALYST.columns.status,
                    dataIndex: 'status',
                    render: (s: string) => <Tag color={STATUS_COLOR[s]}>{ANALYST.status[s]}</Tag>,
                  },
                  {
                    title: ANALYST.columns.started,
                    dataIndex: 'created_at',
                    render: (d: string) => new Date(d).toLocaleString(),
                  },
                  {
                    title: ANALYST.columns.cost,
                    dataIndex: 'cost_usd',
                    render: (usd: number, r) => (r.backend === 'pack' ? '$0' : `$${usd.toFixed(4)}`),
                  },
                  {
                    title: ANALYST.columns.check,
                    dataIndex: 'verification',
                    render: (_v, r) =>
                      r.verification ? (
                        <Tag color={r.verification.passed ? 'success' : 'warning'}>
                          {r.verification.passed ? ANALYST.verified : ANALYST.needsReview}
                        </Tag>
                      ) : (
                        ANALYST.noCheck
                      ),
                  },
                  {
                    title: '',
                    key: 'open',
                    render: (_v, r) => (
                      <Button size="small" onClick={() => setRunId(r.run_id)}>
                        {ANALYST.view}
                      </Button>
                    ),
                  },
                ]}
              />
            )
          }
        </QueryState>
      </Card>
      <CardDrawer
        project={project}
        token={token}
        cardId={cardId}
        onClose={() => setCardId(null)}
        onStarted={(run) => {
          setCardId(null);
          setRunId(run.run_id);
        }}
      />
      <RunDrawer project={project} runId={runId} titles={titles} onClose={() => setRunId(null)} />
    </Space>
  );
}
