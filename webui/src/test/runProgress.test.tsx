import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ThemeProvider } from '../App';
import type { Project, RunState, Site } from '../api/projects';
import type { BatchesPage, LogChunk, UrlsPage } from '../api/runProgress';
import { RunProgressPanel } from '../routes/project/RunProgressPanel';
import { mockApi } from './utils';

const site: Site = {
  site_id: 'flynn-s',
  name: "Flynn's",
  role: 'client',
  main: true,
  domains: ['flynns.com'],
  note: '',
  tags: [],
  url_count: 7,
  last_run: null,
  namespace: 'rankuno-dev.print-security-a1b2c3.flynn-s',
};

const project: Project = {
  project_id: 'print-security-a1b2c3',
  name: 'Print security',
  owner_name: 'Gaurav',
  client_name: "Flynn's",
  created_at: '2026-10-30T09:00:00Z',
  updated_at: '2026-10-30T09:00:00Z',
  url_count: 7,
  sites: [site],
  last_run: null,
};

const run: RunState = {
  run_id: 'flynn-s_20261110-120000',
  project_id: project.project_id,
  site_id: site.site_id,
  status: 'running',
  created_at: '2026-11-10T12:00:00Z',
  started_at: '2026-11-10T12:00:00Z',
  finished_at: null,
  started_by: 'Gaurav',
  url_count: 7,
  batch_size: 3,
  batches_total: 3,
  batches_done: 1,
  current_batch: 2,
  current_attempt: 2,
  pages_done: 4,
  results: { VERIFIED_WITHIN_SCOPE: 2, PARTIAL: 1, NEEDS_REVIEW: 1 },
  message: 'Batch 2 of 3, try 2 for 2 unfinished pages',
  reports_ready: false,
  knowledge_status: '',
  knowledge_message: '',
  on_turn: 'auto',
  email_when_done: false,
  notify_to: '',
  jump: false,
  turn_at: null,
};

const batches: BatchesPage = {
  run_id: run.run_id,
  status: 'running',
  batch_size: 3,
  total: 3,
  offset: 0,
  current_batch: 2,
  items: [
    {
      number: 1,
      state: 'done',
      attempts: 1,
      attempt: 1,
      pages: 3,
      pages_finished: 3,
      by_badge: { VERIFIED_WITHIN_SCOPE: 2, PARTIAL: 1 },
      started_at: '2026-11-10T12:00:10Z',
      finished_at: '2026-11-10T12:04:00Z',
      in_progress: null,
    },
    {
      number: 2,
      state: 'retrying',
      attempts: 2,
      attempt: 2,
      pages: 3,
      pages_finished: 1,
      by_badge: { NEEDS_REVIEW: 1 },
      started_at: '2026-11-10T12:04:01Z',
      finished_at: null,
      in_progress: { pages: 2, fetched: 1, rendered: 1 },
    },
    {
      number: 3,
      state: 'waiting',
      attempts: 0,
      attempt: null,
      pages: 1,
      pages_finished: 0,
      by_badge: {},
      started_at: null,
      finished_at: null,
      in_progress: null,
    },
  ],
};

const urlsOf: Record<string, UrlsPage> = {
  '1': {
    batch: 1,
    total: 3,
    offset: 0,
    items: [
      { url: 'https://www.flynns.com/a', stage: 'done', badge: 'VERIFIED_WITHIN_SCOPE', reason: 'rendered', completeness: 100, gaps: 0, attempt: 1 },
      { url: 'https://www.flynns.com/b', stage: 'done', badge: 'PARTIAL', reason: 'lead_gen_form_wall', completeness: 75, gaps: 2, attempt: 1 },
      { url: 'https://www.flynns.com/c.svg', stage: 'done', badge: 'EXCLUDED', reason: 'image_extracted', completeness: null, gaps: 0, attempt: 1 },
    ],
  },
  '2': {
    batch: 2,
    total: 3,
    offset: 0,
    items: [
      { url: 'https://www.flynns.com/d', stage: 'done', badge: 'NEEDS_REVIEW', reason: 'unchanged:http_304_not_modified', completeness: 50, gaps: 1, attempt: 1 },
      { url: 'https://www.flynns.com/e', stage: 'rendered', badge: null, reason: '', completeness: null, gaps: 0, attempt: null },
      { url: 'https://www.flynns.com/f', stage: 'downloaded', badge: 'FAILED', reason: 'transport_error', completeness: null, gaps: 0, attempt: 1 },
    ],
  },
};

const line = (message: string, level = 'INFO', extra: Record<string, unknown> = {}) => ({
  time: '2026-11-10T12:05:00+00:00',
  level,
  message,
  extra,
});

/** The log grows between calls: the first look gets two lines, the next look one more. */
function logServer(): (url: URL) => LogChunk {
  return (url) => {
    const after = Number(url.searchParams.get('after') ?? '0');
    if (after === 0) {
      return { batch: 2, attempt: 2, exists: true, offset: 300, size: 300, lines: [line('run_execution_started'), line('explore_click_failed', 'INFO', { url: 'https://www.flynns.com/e', control: 'Show more' })] };
    }
    if (after === 300) {
      return { batch: 2, attempt: 2, exists: true, offset: 450, size: 450, lines: [line('image_open_error', 'ERROR')] };
    }
    return { batch: 2, attempt: 2, exists: true, offset: after, size: after, lines: [] };
  };
}

function renderPanel(runs: RunState[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <RunProgressPanel project={project} site={site} runs={runs} />
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

const base = `/api/projects/${project.project_id}/sites/${site.site_id}/runs/${run.run_id}`;

afterEach(() => vi.unstubAllGlobals());

const FIRST_PAINT = { timeout: 15_000 };

describe('live progress of a scrape', () => {
  it('shows the batches, opens the running batch and switches to another on click', async () => {
    const calls = mockApi({
      [`${base}/batches`]: () => batches,
      [`${base}/batches/(\\d+)/urls`]: (url) => urlsOf[url.pathname.split('/').at(-2) ?? ''] ?? { batch: 0, total: 0, offset: 0, items: [] },
    });
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPanel([run]);
    expect(await screen.findByText('Batches and pages', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(await screen.findByText('Trying again', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(screen.getByText('1 of 2 downloaded · 1 rendered')).toBeInTheDocument();
    expect(screen.getByText('Waiting')).toBeInTheDocument();

    // The batch being scraped is open by itself: its pages, stages and results.
    expect(await screen.findByText('Pages of batch 2', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(screen.getByText('https://www.flynns.com/e')).toBeInTheDocument();
    expect(screen.getByText('Rendered in a browser')).toBeInTheDocument();
    expect(screen.getByText('Downloaded')).toBeInTheDocument();
    expect(screen.getByText('Not finished yet')).toBeInTheDocument();
    expect(screen.getByText('Unchanged since the last scrape')).toBeInTheDocument();
    expect(screen.getByLabelText('Content captured: 50%')).toBeInTheDocument();
    expect(calls.some((u) => u.pathname === `${base}/batches/2/urls`)).toBe(true);

    await user.click(screen.getByText('#1'));
    expect(await screen.findByText('Pages of batch 1', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(screen.getByText('https://www.flynns.com/b')).toBeInTheDocument();
    expect(screen.getByText('Behind a form')).toBeInTheDocument();
    expect(screen.getByLabelText('Content captured: 75%')).toBeInTheDocument();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  }, 60_000);

  it('tails the live log in the drawer, in plain words, and keeps appending', async () => {
    mockApi({
      [`${base}/batches`]: () => batches,
      [`${base}/batches/(\\d+)/urls`]: () => urlsOf['2'],
      [`${base}/log`]: logServer(),
    });
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPanel([run]);
    await screen.findByText('Batches and pages', {}, FIRST_PAINT);
    await user.click(screen.getByText('Live log').closest('button') as HTMLElement);
    const log = await screen.findByRole('log', {}, FIRST_PAINT);
    expect(await screen.findByText('Log of batch 2, try 2', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(await within(log).findByText('The batch started')).toBeInTheDocument();
    expect(within(log).getByText('A control on the page could not be clicked')).toBeInTheDocument();
    expect(within(log).getByText(/control: Show more/)).toBeInTheDocument();
    // The next poll (2 s) brings the new line.
    expect(await within(log).findByText('An image could not be opened', {}, { timeout: 8_000 })).toBeInTheDocument();
    expect(within(log).getByText('Error')).toBeInTheDocument();

    await user.click(screen.getByText('Problems only'));
    await waitFor(() => expect(within(log).queryByText('The batch started')).not.toBeInTheDocument());
    expect(within(log).getByText('An image could not be opened')).toBeInTheDocument();

    await user.click(screen.getByText('Pause').closest('button') as HTMLElement);
    expect(screen.getByText('Follow')).toBeInTheDocument();
  }, 60_000);

  it('folds earlier scrapes and shows nothing without any scrape', () => {
    mockApi({ [`${base}/batches`]: () => batches });
    const finished: RunState = { ...run, status: 'finished', finished_at: '2026-11-10T13:00:00Z', current_batch: null, current_attempt: null };
    renderPanel([finished]);
    expect(screen.getByText('Batches and pages of earlier scrapes')).toBeInTheDocument();
    expect(screen.queryByText('Batches and pages')).not.toBeInTheDocument();
    const { container } = renderPanel([]);
    expect(container.textContent).toBe('');
  });
});
