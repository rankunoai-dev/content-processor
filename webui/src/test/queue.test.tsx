import { screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectTokens, type Project, type QueueItem, type RunState, type SharedQueue } from '../api/projects';
import { renderApp } from './utils';

const run = (over: Partial<RunState>): RunState => ({
  run_id: 'r1',
  project_id: 'alpha-1',
  site_id: 'alpha',
  status: 'queued',
  created_at: '2026-11-03T09:00:00Z',
  started_at: null,
  finished_at: null,
  started_by: 'Asha',
  url_count: 120,
  batch_size: 25,
  batches_total: 5,
  batches_done: 0,
  current_batch: null,
  current_attempt: null,
  pages_done: 0,
  results: {},
  message: 'Waiting for its turn in the queue.',
  reports_ready: false,
  knowledge_status: '',
  knowledge_message: '',
  on_turn: 'auto',
  email_when_done: false,
  notify_to: '',
  jump: false,
  turn_at: null,
  ...over,
});

const item = (r: RunState, project_name: string, site_name: string, position: number, wait_s: number): QueueItem => ({
  run: r,
  project_name,
  site_name,
  position,
  wait_s,
});

const queue: SharedQueue = {
  running: [item(run({ run_id: 'r0', status: 'running', pages_done: 400, url_count: 1000, started_by: 'Ravi' }), 'Big site', 'Bigco', 0, 0)],
  waiting: [
    item(run({ run_id: 'r1', on_turn: 'notify', notify_to: 'a***@example.com' }), 'Alpha', 'Alpha', 1, 3600),
    item(run({ run_id: 'r2', project_id: 'beta-1', site_id: 'beta', started_by: 'Bo' }), 'Beta', 'Beta', 2, 7200),
  ],
  ready: [],
  seconds_per_url: 6,
  email_sends: false,
  ready_hours: 72,
};

const project: Project = {
  project_id: 'alpha-1',
  name: 'Alpha',
  owner_name: 'Asha',
  confidential: false,
  client_name: 'Alpha',
  created_at: '2026-11-03T09:00:00Z',
  updated_at: '2026-11-03T09:00:00Z',
  url_count: 3,
  sites: [
    { site_id: 'alpha', name: 'Alpha', role: 'client', main: true, domains: ['alpha.com'], note: '', tags: [], url_count: 3, last_run: null, namespace: 'n.alpha' },
  ],
  last_run: null,
};

interface Sent {
  path: string;
  body: unknown;
}

function fakeServer(routes: Record<string, (body: unknown) => [number, unknown]>) {
  const sent: Sent[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1');
      const method = init?.method ?? 'GET';
      const body: unknown = init?.body ? JSON.parse(String(init.body)) : undefined;
      if (method === 'POST') sent.push({ path: url.pathname, body });
      const handler = routes[`${method} ${url.pathname}`];
      const [status, payload] = handler ? handler(body) : [404, { detail: 'There is no such thing.' }];
      return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
    }),
  );
  return sent;
}

const FIRST_PAINT = { timeout: 15_000 };

beforeEach(() => {
  useProjectTokens.setState({ tokens: {} });
  window.localStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

describe('the shared scrape queue', () => {
  it('shows what runs, who waits in which order and roughly how long', async () => {
    fakeServer({ 'GET /api/queue': () => [200, queue] });
    renderApp('/queue');
    expect(await screen.findByText('Bigco', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(screen.getByText(/400 of 1,000 pages/)).toBeInTheDocument();
    expect(screen.getByText('Waiting in line (2)')).toBeInTheDocument();
    expect(screen.getByText('1 h')).toBeInTheDocument();
    expect(screen.getByText('2 h')).toBeInTheDocument();
    expect(screen.getByText('Emails a***@example.com, then waits')).toBeInTheDocument();
    expect(screen.getByText(/about 6 seconds per address/)).toBeInTheDocument();
  });

  it('queues a scrape that emails its person when its turn comes', async () => {
    const sent = fakeServer({
      'GET /api/queue': () => [200, queue],
      'GET /api/projects/alpha-1': () => [200, project],
      'GET /api/projects/alpha-1/runs': () => [200, []],
      'GET /api/projects/alpha-1/sites/alpha/urls': () => [200, { total: 0, items: [] }],
      'POST /api/projects/alpha-1/sites/alpha/runs': () => [200, run({ on_turn: 'notify', notify_to: 'a***@example.com' })],
    });
    useProjectTokens.getState().remember('alpha-1', 'tok', '2099-01-01T00:00:00Z');
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderApp('/projects/alpha-1?site=alpha&view=scrape');
    const startText = await screen.findByText('Start scraping: Alpha', {}, FIRST_PAINT);
    await user.click(startText.closest('button') as HTMLElement);
    const dialog = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('.ant-modal-content');
      if (!found) throw new Error('no dialog yet');
      return found;
    }, FIRST_PAINT);
    expect(await within(dialog).findByText(/3 scrapes are ahead in the queue \(about 2 h 12 min\)/)).toBeInTheDocument();
    await user.click(within(dialog).getByText('Email me, and I start it'));
    await user.click(within(dialog).getByText('Add to the queue').closest('button') as HTMLElement);
    expect(await within(dialog).findByText('Give an email address to be told by email.')).toBeInTheDocument();
    expect(within(dialog).getByText(/Email is not set up on this server yet/)).toBeInTheDocument();
    await user.click(within(dialog).getByLabelText('Your email'));
    await user.paste('asha@example.com');
    await user.click(within(dialog).getByText('Also email me when it finishes'));
    await user.click(within(dialog).getByText('Add to the queue').closest('button') as HTMLElement);
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({ on_turn: 'notify', email: 'asha@example.com', email_when_done: true });
    expect(window.localStorage.getItem('cp.queue.email')).toBe('asha@example.com');
  }, 120_000);
});
