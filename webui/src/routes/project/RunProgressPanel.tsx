/**
 * Live progress of a scrape inside the project page: the batches, the pages of one
 * batch (stage reached, result, how much content was captured) and a live log.
 *
 * Shown open for the scrape that runs now and, folded, for earlier scrapes. Batches are
 * refreshed every 3 s while the scrape is active (15 s otherwise), the pages of a batch
 * on demand (every 3 s for the batch being scraped) and the log every 2 s, but only
 * while its drawer is open.
 */
import { FileTextOutlined, PauseCircleOutlined, PlayCircleOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Collapse, Drawer, Progress, Segmented, Space, Table, Tag, Tooltip, Typography } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { ACTIVE, type Project, type RunState, type Site } from '../../api/projects';
import {
  BATCHES_PER_PAGE,
  LOG_MAX_LINES,
  LOG_POLL_MS,
  URLS_PER_PAGE,
  runProgressApi,
  useBatchUrls,
  useRunBatches,
  type BatchProgress,
  type LogLine,
  type UrlRow,
} from '../../api/runProgress';
import { StatusBar } from '../../components/StatusBar';
import { HelpTip, StatusTag } from '../../components/bits';
import { PROJECTS, describeLogMessage, describeStageReason, formatDate } from '../../vocabulary';

const TEXT = PROJECTS.progressView;

const STATE_COLOR: Record<string, string> = {
  waiting: 'default',
  running: 'processing',
  retrying: 'warning',
  done: 'success',
};

const STAGE_COLOR: Record<string, string> = {
  queued: 'default',
  downloaded: 'blue',
  rendered: 'geekblue',
  done: 'green',
};

const LEVEL_COLOR: Record<string, string> = {
  WARNING: 'warning',
  ERROR: 'error',
  CRITICAL: 'error',
};

const PROBLEM_LEVELS = new Set(['WARNING', 'ERROR', 'CRITICAL']);

function clock(iso: string | null): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** The 1-based page of the batch table that holds this batch. */
function pageOf(batch: number | null): number {
  return batch ? Math.floor((batch - 1) / BATCHES_PER_PAGE) + 1 : 1;
}

// -- the pages of one batch -----------------------------------------------------------

function UrlTable({
  project,
  site,
  run,
  batch,
  live,
}: {
  project: Project;
  site: Site;
  run: RunState;
  batch: number;
  live: boolean;
}) {
  const [page, setPage] = useState(1);
  const urls = useBatchUrls(project.project_id, site.site_id, run.run_id, batch, page, live);
  return (
    <Card
      size="small"
      title={
        <Space size={6}>
          {TEXT.pagesOf(batch)}
          <HelpTip text={TEXT.completenessHelp} />
        </Space>
      }
    >
      {urls.error ? <Alert type="warning" showIcon message={urls.error.message} style={{ marginBottom: 8 }} /> : null}
      <Table<UrlRow>
        rowKey="url"
        size="small"
        loading={urls.isLoading}
        dataSource={urls.data?.items ?? []}
        scroll={{ x: true }}
        pagination={{
          current: page,
          pageSize: URLS_PER_PAGE,
          total: urls.data?.total ?? 0,
          showSizeChanger: false,
          hideOnSinglePage: true,
          onChange: setPage,
        }}
        columns={[
          {
            title: TEXT.url,
            dataIndex: 'url',
            ellipsis: true,
            render: (url: string) => (
              <a href={url} target="_blank" rel="noopener noreferrer">
                {url}
              </a>
            ),
          },
          {
            title: TEXT.stage,
            dataIndex: 'stage',
            width: 170,
            render: (stage: string) => (
              <Tag color={STAGE_COLOR[stage]} bordered={false}>
                {TEXT.stages[stage] ?? stage}
              </Tag>
            ),
          },
          {
            title: TEXT.result,
            width: 190,
            render: (_, r) =>
              r.badge ? (
                <Tooltip title={r.stage !== 'done' && r.attempt ? TEXT.earlierTry(r.attempt) : undefined}>
                  <span style={{ opacity: r.stage === 'done' ? 1 : 0.6 }}>
                    <StatusTag badge={r.badge} />
                  </span>
                </Tooltip>
              ) : (
                <Typography.Text type="secondary">{TEXT.notYet}</Typography.Text>
              ),
          },
          {
            title: (
              <Space size={4}>
                {TEXT.completeness}
                <HelpTip text={TEXT.completenessHelp} />
              </Space>
            ),
            width: 170,
            render: (_, r) =>
              r.completeness === null ? (
                <Tooltip title={TEXT.completenessHelp}>
                  <Typography.Text type="secondary">{TEXT.noCompleteness}</Typography.Text>
                </Tooltip>
              ) : (
                <Tooltip title={TEXT.completenessHelp}>
                  <Progress
                    percent={r.completeness}
                    size="small"
                    status={r.completeness === 100 ? 'success' : 'normal'}
                    aria-label={`${TEXT.completeness}: ${r.completeness}%`}
                  />
                </Tooltip>
              ),
          },
          {
            title: TEXT.reason,
            dataIndex: 'reason',
            ellipsis: true,
            render: (reason: string) => (
              <Tooltip title={reason || undefined}>
                <span>{describeStageReason(reason)}</span>
              </Tooltip>
            ),
          },
          {
            title: TEXT.gaps,
            dataIndex: 'gaps',
            width: 110,
            align: 'right',
            render: (n: number, r) => (r.stage === 'done' ? n.toLocaleString() : ''),
          },
        ]}
      />
    </Card>
  );
}

// -- the live log ---------------------------------------------------------------------

function LogLineView({ line }: { line: LogLine }) {
  const extras = Object.entries(line.extra).filter(([, v]) => v !== null && v !== undefined && v !== '');
  const problem = PROBLEM_LEVELS.has(line.level);
  return (
    <div className="run-log-line" style={{ padding: '2px 0', borderBottom: '1px solid rgba(128,128,128,0.12)' }}>
      <Space size={6} wrap>
        <Typography.Text type="secondary" style={{ fontVariantNumeric: 'tabular-nums' }}>
          {clock(line.time)}
        </Typography.Text>
        {problem ? (
          <Tag color={LEVEL_COLOR[line.level]} bordered={false}>
            {line.level === 'WARNING' ? 'Warning' : 'Error'}
          </Tag>
        ) : null}
        <Typography.Text type={line.level === 'TEXT' ? 'secondary' : undefined} code={line.level === 'TEXT'}>
          {line.level === 'TEXT' ? line.message : describeLogMessage(line.message)}
        </Typography.Text>
      </Space>
      {extras.length > 0 ? (
        <div>
          {extras.map(([k, v]) => (
            <Typography.Text key={k} type="secondary" style={{ marginRight: 12, wordBreak: 'break-all' }}>
              {k}: {typeof v === 'string' ? v : JSON.stringify(v)}
            </Typography.Text>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function LogDrawer({
  project,
  site,
  run,
  batch,
  attempt,
  active,
  open,
  onClose,
}: {
  project: Project;
  site: Site;
  run: RunState;
  batch: number | null;
  attempt: number | null;
  active: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [exists, setExists] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [level, setLevel] = useState<'all' | 'problems'>('all');
  const bodyRef = useRef<HTMLDivElement>(null);
  const id = project.project_id;
  const siteId = site.site_id;
  const runId = run.run_id;

  useEffect(() => {
    if (!open || batch === null || attempt === null) return undefined;
    let cancelled = false;
    let after = 0;
    const controller = new AbortController();
    setLines([]);
    setError(null);
    const tick = async () => {
      try {
        // Catch up in a few calls when the log grew more than one chunk since the last look.
        for (let calls = 0; calls < 10 && !cancelled; calls += 1) {
          const chunk = await runProgressApi.log(id, siteId, runId, batch, attempt, after, controller.signal);
          if (cancelled) return;
          after = chunk.offset;
          setExists(chunk.exists);
          if (chunk.lines.length > 0) {
            setLines((previous) => [...previous, ...chunk.lines].slice(-LOG_MAX_LINES));
          }
          if (!chunk.exists || chunk.offset >= chunk.size) break;
        }
        setError(null);
      } catch (e) {
        if (!cancelled && !(e instanceof DOMException && e.name === 'AbortError')) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    };
    void tick();
    const timer = active ? window.setInterval(() => void tick(), LOG_POLL_MS) : null;
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) window.clearInterval(timer);
    };
  }, [open, batch, attempt, active, id, siteId, runId]);

  useEffect(() => {
    if (!paused && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [lines, paused]);

  const shown = level === 'all' ? lines : lines.filter((l) => PROBLEM_LEVELS.has(l.level));
  return (
    <Drawer
      title={batch !== null && attempt !== null ? TEXT.logTitle(batch, attempt) : TEXT.liveLog}
      open={open}
      onClose={onClose}
      width={620}
      extra={
        <Space>
          <Segmented
            size="small"
            value={level}
            onChange={(v) => setLevel(v as 'all' | 'problems')}
            options={[
              { value: 'all', label: TEXT.levels.all },
              { value: 'problems', label: TEXT.levels.problems },
            ]}
          />
          <Button
            size="small"
            icon={paused ? <PlayCircleOutlined /> : <PauseCircleOutlined />}
            onClick={() => setPaused((p) => !p)}
          >
            {paused ? TEXT.follow : TEXT.pause}
          </Button>
        </Space>
      }
    >
      <Typography.Paragraph type="secondary">{TEXT.logHelp}</Typography.Paragraph>
      {error ? <Alert type="warning" showIcon message={error} style={{ marginBottom: 8 }} /> : null}
      <div
        ref={bodyRef}
        role="log"
        aria-live="polite"
        aria-label={TEXT.liveLog}
        style={{ maxHeight: 'calc(100vh - 200px)', overflowY: 'auto', fontSize: 13 }}
      >
        {batch === null || attempt === null ? (
          <Typography.Text type="secondary">{TEXT.logNoBatch}</Typography.Text>
        ) : !exists ? (
          <Typography.Text type="secondary">{TEXT.logMissing}</Typography.Text>
        ) : shown.length === 0 ? (
          <Typography.Text type="secondary">{TEXT.logEmpty}</Typography.Text>
        ) : (
          shown.map((line, i) => <LogLineView key={`${i}-${line.time}`} line={line} />)
        )}
      </div>
    </Drawer>
  );
}

// -- one scrape -----------------------------------------------------------------------

export function RunProgress({ project, site, run, active }: { project: Project; site: Site; run: RunState; active: boolean }) {
  const [page, setPage] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const currentPage = page ?? pageOf(run.current_batch);
  const batches = useRunBatches(project.project_id, site.site_id, run.run_id, currentPage, active);
  const current = batches.data?.current_batch ?? (active ? run.current_batch : null);
  const shownBatch = selected ?? current;
  const items = batches.data?.items ?? [];
  const shownItem = items.find((b) => b.number === shownBatch);
  const logBatch = current ?? shownBatch;
  const logAttempt = (logBatch === current ? run.current_attempt : null) ?? items.find((b) => b.number === logBatch)?.attempt ?? null;
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {batches.error ? <Alert type="warning" showIcon message={batches.error.message} /> : null}
      <Table<BatchProgress>
        rowKey="number"
        size="small"
        loading={batches.isLoading}
        dataSource={items}
        scroll={{ x: true }}
        onRow={(b) => ({
          onClick: () => setSelected(b.number),
          style: { cursor: 'pointer' },
          'aria-selected': b.number === shownBatch,
        })}
        rowClassName={(b) => (b.number === shownBatch ? 'ant-table-row-selected' : '')}
        pagination={{
          current: currentPage,
          pageSize: BATCHES_PER_PAGE,
          total: batches.data?.total ?? 0,
          showSizeChanger: false,
          hideOnSinglePage: true,
          onChange: setPage,
        }}
        columns={[
          { title: TEXT.batch, dataIndex: 'number', width: 80, render: (n: number) => `#${n}` },
          {
            title: TEXT.state,
            dataIndex: 'state',
            width: 260,
            render: (state: string, b) => (
              <Space size={6} wrap>
                <Tag color={STATE_COLOR[state]} bordered={false}>
                  {TEXT.states[state] ?? state}
                </Tag>
                {b.in_progress ? (
                  <Typography.Text type="secondary">{TEXT.inProgress(b.in_progress)}</Typography.Text>
                ) : null}
              </Space>
            ),
          },
          {
            title: TEXT.pagesDone,
            width: 200,
            render: (_, b) => (
              <Space size={8}>
                <Progress
                  percent={b.pages ? Math.round((100 * b.pages_finished) / b.pages) : 0}
                  size="small"
                  showInfo={false}
                  style={{ width: 90, margin: 0 }}
                  status={b.state === 'done' ? 'success' : 'normal'}
                />
                <Typography.Text>
                  {b.pages_finished}/{b.pages}
                </Typography.Text>
              </Space>
            ),
          },
          {
            title: TEXT.results,
            width: 160,
            render: (_, b) => <StatusBar badges={b.by_badge} total={Math.max(b.pages, 1)} />,
          },
          {
            title: TEXT.attempt,
            width: 90,
            render: (_, b) => (b.attempt ? TEXT.attemptOf(b.attempt) : ''),
          },
          {
            title: TEXT.started,
            dataIndex: 'started_at',
            responsive: ['lg'],
            render: (iso: string | null) => (iso ? formatDate(iso, true) : ''),
          },
        ]}
      />
      <Space wrap>
        <Button icon={<FileTextOutlined />} onClick={() => setLogOpen(true)} disabled={logBatch === null}>
          {TEXT.liveLog}
        </Button>
        {shownBatch === null ? <Typography.Text type="secondary">{TEXT.pickBatch}</Typography.Text> : null}
      </Space>
      {shownBatch !== null ? (
        <UrlTable
          project={project}
          site={site}
          run={run}
          batch={shownBatch}
          live={active && shownItem?.state !== 'done' && shownBatch === current}
        />
      ) : null}
      <LogDrawer
        project={project}
        site={site}
        run={run}
        batch={logBatch}
        attempt={logAttempt}
        active={active}
        open={logOpen}
        onClose={() => setLogOpen(false)}
      />
    </Space>
  );
}

// -- the panel on the project page ---------------------------------------------------

const EARLIER_RUNS_SHOWN = 5;

/** Open for the scrape running now; folded, one per earlier scrape, below it. */
export function RunProgressPanel({ project, site, runs }: { project: Project; site: Site; runs: RunState[] }) {
  const active = runs.find((r) => ACTIVE.has(r.status));
  const earlier = runs.filter((r) => !ACTIVE.has(r.status) && r.batches_total > 0).slice(0, EARLIER_RUNS_SHOWN);
  if (!active && earlier.length === 0) return null;
  return (
    <>
      {active ? (
        <Card
          title={
            <Space size={6}>
              {TEXT.title}
              <HelpTip text={TEXT.help} />
            </Space>
          }
        >
          <RunProgress project={project} site={site} run={active} active />
        </Card>
      ) : null}
      {earlier.length > 0 ? (
        <Collapse
          destroyInactivePanel
          items={[
            {
              key: 'earlier',
              label: TEXT.earlier,
              children: (
                <Collapse
                  destroyInactivePanel
                  items={earlier.map((r) => ({
                    key: r.run_id,
                    label: `${formatDate(r.created_at, true)} · ${PROJECTS.status[r.status] ?? r.status} · ${PROJECTS.progress(r.pages_done, r.url_count)}`,
                    children: <RunProgress project={project} site={site} run={r} active={false} />,
                  }))}
                />
              ),
            },
          ]}
        />
      ) : null}
    </>
  );
}

export default RunProgressPanel;
