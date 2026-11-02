import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MonitorCheck, WatchSettings } from '../api/monitoring';
import { useProjectTokens, type Project } from '../api/projects';
import { renderApp } from './utils';

const project: Project = {
  project_id: 'p1',
  name: 'Print security',
  owner_name: 'Gaurav',
  client_name: "Flynn's",
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-01T09:00:00Z',
  url_count: 6,
  sites: [],
  last_run: null,
};

const check: MonitorCheck = {
  check_id: 'check-20261102-101010-001',
  project_id: 'p1',
  created_at: '2026-11-02T10:10:10Z',
  trigger: 'auto',
  websites: [
    {
      website: 'xerox.com',
      name: 'Xerox',
      role: 'competitor',
      baseline_run: 'r1',
      latest_run: 'r2',
      baseline_at: null,
      latest_at: null,
      note: '',
      new_pages: [{ url: 'https://www.xerox.com/new', title: 'New secure print', theme_id: 'th-1', theme: 'Secure print' }],
      removed_pages: [],
      changed_pages: [
        {
          url: 'https://www.xerox.com/pricing',
          title: 'Xerox pricing',
          theme: 'Secure print',
          head_changes: { title: { before: 'Pricing', after: 'Pricing: now cheaper' } },
          sections_added: 1,
          sections_removed: 1,
          added_text: ['Plans start at $19 per month.'],
          removed_text: ['Plans start at $25 per month.'],
          drift: 0.5,
          prices: [{ before: '$25 per month', after: '$19 per month', context: 'plans start at … today' }],
          new_numbers: [],
          is_pillar: true,
        },
      ],
      unchanged_pages: 4,
      template_changes: [{ line: 'Read more', added_on: 5, removed_on: 0 }],
      drift: 0.33,
    },
  ],
  theme_drift: [
    {
      theme_id: 'th-1',
      label: 'Secure print',
      status: 'existing',
      ownership_before: 'contested',
      ownership_after: 'competitor_led',
      new_pages: { 'xerox.com': 3 },
      removed_pages: {},
      pages_before: {},
      pages_after: {},
    },
  ],
  alerts: [
    {
      alert_id: 'al-1',
      severity: 'high',
      kind: 'price_change',
      website: 'xerox.com',
      title: 'Prices changed on 1 page(s) of Xerox',
      detail: '$25 per month → $19 per month',
      theme_id: null,
      urls: [],
    },
  ],
  stats: { new_pages: 1, removed_pages: 0, changed_pages: 1, price_changes: 1 },
};

const watch: WatchSettings = {
  auto_check: true,
  rescrape_every_days: 0,
  push_threshold: 3,
  last_rescrape_at: null,
  last_auto_check_at: null,
};

interface Sent {
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

function fakeServer(routes: Record<string, (body: unknown) => [number, unknown]>) {
  const sent: Sent[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1');
      const method = init?.method ?? 'GET';
      const body: unknown = init?.body ? JSON.parse(String(init.body)) : undefined;
      if (method === 'POST') sent.push({ path: url.pathname, body, headers: (init?.headers ?? {}) as Record<string, string> });
      const handler = routes[`${method} ${url.pathname}`];
      const [status, payload] = handler ? handler(body) : [404, { detail: 'There is no such thing.' }];
      return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
    }),
  );
  return sent;
}

const base = {
  'GET /api/projects/p1': () => [200, project] as [number, unknown],
  'GET /api/projects/p1/runs': () => [200, []] as [number, unknown],
  'GET /api/projects/p1/monitoring/watch': () => [200, watch] as [number, unknown],
};

const FIRST_PAINT = { timeout: 15_000 };
const TIMEOUT = 120_000;

beforeEach(() => useProjectTokens.setState({ tokens: {} }));
afterEach(() => vi.unstubAllGlobals());

describe('monitoring tab', () => {
  it(
    'runs a first check once unlocked and shows alerts, changes and theme drift',
    async () => {
      let ran = false;
      const sent = fakeServer({
        ...base,
        'GET /api/projects/p1/monitoring': () =>
          ran ? [200, check] : [404, { detail: 'No monitoring check has been run yet.' }],
        'POST /api/projects/p1/monitoring': () => {
          ran = true;
          return [200, check];
        },
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects/p1?tab=monitoring');
      expect(await screen.findByText('No check has been run for this project yet.', {}, FIRST_PAINT)).toBeInTheDocument();
      const button = () => screen.getByText('Run a check').closest('button') as HTMLElement;
      expect(button()).toBeDisabled();
      act(() => useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z'));
      await waitFor(() => expect(button()).not.toBeDisabled());
      await user.click(button());
      expect(await screen.findByText('Prices changed on 1 page(s) of Xerox')).toBeInTheDocument();
      expect(sent[0]?.headers['X-Project-Token']).toBe('tok-1');
      expect(screen.getByText(/automatic, after a new scrape/)).toBeInTheDocument();
      expect(screen.getByText('Contested → Competitor leads')).toBeInTheDocument();
      expect(screen.getByText('xerox.com 3')).toBeInTheDocument();
      const row = screen.getByText('Xerox').closest('tr') as HTMLElement;
      await user.click(within(row).getByRole('button'));
      expect(await screen.findByText(/\$25 per month → \$19 per month \(plans start/)).toBeInTheDocument();
      expect(screen.getByText('pillar')).toBeInTheDocument();
      expect(screen.getByText(/“Read more” \(\+5 \/ −0\)/)).toBeInTheDocument();
    },
    TIMEOUT,
  );

  it(
    'saves how the project is watched, only when unlocked',
    async () => {
      const sent = fakeServer({
        ...base,
        'GET /api/projects/p1/monitoring': () => [200, check],
        'POST /api/projects/p1/monitoring/watch': (body) => [200, { ...watch, ...(body as object) }],
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects/p1?tab=monitoring');
      expect(await screen.findByText('How this project is watched', {}, FIRST_PAINT)).toBeInTheDocument();
      expect((await screen.findByText('Save', {}, FIRST_PAINT)).closest('button')).toBeDisabled();
      act(() => useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z'));
      await waitFor(() => expect(screen.getByText('Save').closest('button')).not.toBeDisabled());
      await user.click(screen.getByLabelText('Check automatically after each new scrape'));
      await user.click(screen.getByText('Save').closest('button') as HTMLElement);
      await waitFor(() => expect(sent).toHaveLength(1));
      expect(sent[0]?.body).toEqual({ auto_check: false, rescrape_every_days: 0, push_threshold: 3 });
      expect(screen.getByText(/contacts the client’s and competitors’ websites/)).toBeInTheDocument();
    },
    TIMEOUT,
  );
});
