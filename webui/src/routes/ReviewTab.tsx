/**
 * The project's "Review" tab: every page of a scrape with the engine's verdict; a reviewer
 * approves it, or rejects it with a reason and a next step. Decisions are kept with who
 * and when (the history), and the next scrape and the reports follow them.
 */
import { CheckOutlined, CloseOutlined } from '@ant-design/icons';
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Popover,
  Radio,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd';
import { useMemo, useState } from 'react';
import { useCan, useMe } from '../api/auth';
import { useProjectRuns, useProjectToken, type Project } from '../api/projects';
import { useSaveVerdict, useVerdicts, type NextStep, type PageVerdict } from '../api/verdicts';
import { QueryState } from '../components/QueryState';
import { StatusTag } from '../components/bits';
import { REVIEW_TAB, formatDate } from '../vocabulary';

type Show = 'attention' | 'undecided' | 'decided' | 'all';
const NAME_KEY = 'capture-results-reviewer';

function readName(): string {
  try {
    return window.localStorage.getItem(NAME_KEY) ?? '';
  } catch {
    return '';
  }
}

function DecisionCell({ page }: { page: PageVerdict }) {
  const d = page.decision;
  if (!d) return <Typography.Text type="secondary">{REVIEW_TAB.undecided}</Typography.Text>;
  const head =
    d.decision === 'approved' ? (
      <Tag color="green">{REVIEW_TAB.approved}</Tag>
    ) : (
      <Tag color="red">{REVIEW_TAB.nextSteps[d.next_step] ?? REVIEW_TAB.rejected}</Tag>
    );
  const history = (
    <Space direction="vertical" size={4} style={{ maxWidth: 360 }}>
      {page.history.map((h, i) => (
        <Typography.Text key={`${h.at}${i}`} type={i === 0 ? undefined : 'secondary'}>
          {formatDate(h.at)} · {h.reviewer}:{' '}
          {h.decision === 'approved'
            ? REVIEW_TAB.approved
            : h.decision === 'rejected'
              ? `${REVIEW_TAB.nextSteps[h.next_step] ?? REVIEW_TAB.rejected} (${h.note})`
              : REVIEW_TAB.cleared}
        </Typography.Text>
      ))}
    </Space>
  );
  return (
    <Space direction="vertical" size={0}>
      <Space size={4} wrap>
        {head}
        <Popover title={REVIEW_TAB.history} content={history}>
          <Typography.Link>{REVIEW_TAB.byWhen(d.reviewer, formatDate(d.at))}</Typography.Link>
        </Popover>
      </Space>
      {d.note ? <Typography.Text type="secondary">{d.note}</Typography.Text> : null}
    </Space>
  );
}

function RejectModal({
  page,
  busy,
  onCancel,
  onSave,
}: {
  page: PageVerdict | null;
  busy: boolean;
  onCancel: () => void;
  onSave: (v: { note: string; next_step: NextStep }) => void;
}) {
  const [form] = Form.useForm<{ note: string; next_step: NextStep }>();
  return (
    <Modal
      title={REVIEW_TAB.rejectTitle}
      open={page !== null}
      onCancel={onCancel}
      onOk={() => form.submit()}
      okText={REVIEW_TAB.reject}
      okButtonProps={{ danger: true }}
      confirmLoading={busy}
      destroyOnClose
    >
      <Typography.Paragraph type="secondary" style={{ overflowWrap: 'anywhere' }}>
        {page?.title ?? page?.url}
      </Typography.Paragraph>
      <Form form={form} layout="vertical" requiredMark={false} onFinish={onSave}>
        <Form.Item
          name="note"
          label={REVIEW_TAB.reason}
          rules={[{ required: true, whitespace: true, message: REVIEW_TAB.reasonNeeded }]}
        >
          <Input.TextArea rows={3} maxLength={2000} placeholder={REVIEW_TAB.reasonPlaceholder} />
        </Form.Item>
        <Form.Item
          name="next_step"
          label={REVIEW_TAB.nextStep}
          rules={[{ required: true, message: REVIEW_TAB.nextStepNeeded }]}
        >
          <Radio.Group>
            <Space direction="vertical">
              {(['scrape_again', 'obtain_manually', 'exclude'] as NextStep[]).map((s) => (
                <Radio key={s} value={s}>
                  <Typography.Text strong>{REVIEW_TAB.nextSteps[s]}</Typography.Text>{' '}
                  <Typography.Text type="secondary">{REVIEW_TAB.nextStepHelp[s]}</Typography.Text>
                </Radio>
              ))}
            </Space>
          </Radio.Group>
        </Form.Item>
      </Form>
    </Modal>
  );
}

export function ReviewTab({ project }: { project: Project }) {
  const runs = useProjectRuns(project.project_id);
  const me = useMe();
  const accountsOn = me.data?.accounts === true;
  const canReview = useCan('reviewer');
  const token = useProjectToken(project.project_id);
  const allowed = accountsOn ? canReview : token !== null;
  const { message } = AntApp.useApp();
  const finished = useMemo(
    () =>
      (runs.data ?? [])
        .filter((r) => r.status === 'finished')
        .sort((a, b) => (a.created_at < b.created_at ? 1 : -1)),
    [runs.data],
  );
  const sitesWithRuns = project.sites.filter((s) => finished.some((r) => r.site_id === s.site_id));
  const [siteId, setSiteId] = useState<string | null>(null);
  const site = siteId ?? sitesWithRuns[0]?.site_id ?? null;
  const siteRuns = finished.filter((r) => r.site_id === site);
  const [runId, setRunId] = useState<string | null>(null);
  const run = runId && siteRuns.some((r) => r.run_id === runId) ? runId : (siteRuns[0]?.run_id ?? null);
  const verdicts = useVerdicts(project.project_id, site, run);
  const save = useSaveVerdict(project.project_id, site ?? '', run ?? '');
  const [show, setShow] = useState<Show>('attention');
  const [files, setFiles] = useState(false);
  const [rejecting, setRejecting] = useState<PageVerdict | null>(null);
  const [name, setName] = useState(readName);

  const send = (page: PageVerdict, body: { decision: 'approved' | 'rejected' | ''; next_step?: NextStep; note?: string }) => {
    if (!accountsOn && !name.trim()) {
      void message.warning(REVIEW_TAB.nameFirst);
      return;
    }
    save.mutate(
      { body: { url: page.url, ...body, ...(accountsOn ? {} : { reviewer: name.trim() }) }, token },
      {
        onSuccess: () => {
          setRejecting(null);
          void message.success(REVIEW_TAB.saved);
        },
        onError: (e) => void message.error(e.message),
      },
    );
  };

  if (runs.isSuccess && sitesWithRuns.length === 0) return <Empty description={REVIEW_TAB.noScrape} />;

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert type="info" showIcon message={REVIEW_TAB.intro} />
      <Space wrap>
        <Select
          aria-label={REVIEW_TAB.website}
          style={{ minWidth: 220 }}
          value={site ?? undefined}
          onChange={(v) => {
            setSiteId(v);
            setRunId(null);
          }}
          options={sitesWithRuns.map((s) => ({ value: s.site_id, label: s.name }))}
        />
        <Select
          aria-label={REVIEW_TAB.scrape}
          style={{ minWidth: 220 }}
          value={run ?? undefined}
          onChange={setRunId}
          options={siteRuns.map((r) => ({ value: r.run_id, label: formatDate(r.created_at) }))}
        />
        {accountsOn ? null : (
          <Input
            aria-label={REVIEW_TAB.yourName}
            placeholder={REVIEW_TAB.yourName}
            style={{ width: 200 }}
            value={name}
            maxLength={80}
            onChange={(e) => {
              setName(e.target.value);
              try {
                window.localStorage.setItem(NAME_KEY, e.target.value);
              } catch {
                // private window: the name is kept for this page only
              }
            }}
          />
        )}
      </Space>
      {!allowed ? (
        <Typography.Text type="secondary">{accountsOn ? REVIEW_TAB.roleNeeded : REVIEW_TAB.unlockNeeded}</Typography.Text>
      ) : null}
      <QueryState query={verdicts} rows={6}>
        {(list) => {
          const pages = list.pages.filter((p) => (files || p.is_page) && (
            show === 'all' ||
            (show === 'undecided' && !p.decision) ||
            (show === 'decided' && p.decision) ||
            (show === 'attention' && (p.badge !== 'VERIFIED_WITHIN_SCOPE' || p.decision?.decision === 'rejected'))
          ));
          return (
            <Card size="small">
              <Space direction="vertical" style={{ width: '100%' }}>
                <div style={{ maxWidth: '100%', overflowX: 'auto' }}>
                  <Segmented<Show>
                    value={show}
                    onChange={setShow}
                    options={(['attention', 'undecided', 'decided', 'all'] as Show[]).map((s) => ({
                      value: s,
                      label: REVIEW_TAB.show[s],
                    }))}
                  />
                </div>
                <Space wrap>
                  <Space size={6}>
                    <Switch size="small" checked={files} onChange={setFiles} aria-label={REVIEW_TAB.files} />
                    <Typography.Text type="secondary">{REVIEW_TAB.files}</Typography.Text>
                  </Space>
                  <Typography.Text type="secondary">
                    {REVIEW_TAB.summary(list.counts.undecided ?? 0, list.counts['decision:approved'] ?? 0)}
                  </Typography.Text>
                </Space>
                <Table<PageVerdict>
                  rowKey="url"
                  size="small"
                  scroll={{ x: true }}
                  pagination={{ pageSize: 25, hideOnSinglePage: true }}
                  dataSource={pages}
                  locale={{ emptyText: REVIEW_TAB.nothing }}
                  columns={[
                    {
                      title: REVIEW_TAB.page,
                      render: (_v, p) => (
                        <Space direction="vertical" size={0} style={{ maxWidth: 420 }}>
                          <Typography.Text strong>{p.title ?? p.url}</Typography.Text>
                          <a href={p.url} target="_blank" rel="noreferrer noopener" style={{ overflowWrap: 'anywhere' }}>
                            {p.url}
                          </a>
                        </Space>
                      ),
                    },
                    { title: REVIEW_TAB.verdict, render: (_v, p) => <StatusTag badge={p.badge} /> },
                    {
                      title: REVIEW_TAB.issues,
                      render: (_v, p) =>
                        p.gaps.length ? (
                          <Typography.Text type="secondary">{p.gaps.join(', ').toLowerCase().replaceAll('_', ' ')}</Typography.Text>
                        ) : (
                          '-'
                        ),
                    },
                    { title: REVIEW_TAB.decision, render: (_v, p) => <DecisionCell page={p} /> },
                    {
                      title: '',
                      render: (_v, p) => (
                        <Space size={4} wrap>
                          <Button
                            size="small"
                            icon={<CheckOutlined />}
                            disabled={!allowed || p.decision?.decision === 'approved'}
                            aria-label={`${REVIEW_TAB.approve}: ${p.url}`}
                            onClick={() => send(p, { decision: 'approved' })}
                          >
                            {REVIEW_TAB.approve}
                          </Button>
                          <Button
                            size="small"
                            danger
                            icon={<CloseOutlined />}
                            disabled={!allowed}
                            aria-label={`${REVIEW_TAB.reject}: ${p.url}`}
                            onClick={() => setRejecting(p)}
                          >
                            {REVIEW_TAB.reject}
                          </Button>
                          {p.decision ? (
                            <Button size="small" type="link" disabled={!allowed} onClick={() => send(p, { decision: '' })}>
                              {REVIEW_TAB.clear}
                            </Button>
                          ) : null}
                        </Space>
                      ),
                    },
                  ]}
                />
              </Space>
            </Card>
          );
        }}
      </QueryState>
      <RejectModal
        page={rejecting}
        busy={save.isPending}
        onCancel={() => setRejecting(null)}
        onSave={(v) => rejecting && send(rejecting, { decision: 'rejected', ...v })}
      />
    </Space>
  );
}
