import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { GapReport } from '../api/gaps';
import { useProjectTokens, type Project } from '../api/projects';
import type { CompetitorBook, CompetitorSuggestion, ResearchSnapshot, ResearchStatus } from '../api/research';
import { renderApp } from './utils';

const project: Project = {
  project_id: 'p1',
  name: 'Life cover',
  owner_name: 'Gaurav',
  confidential: false,
  client_name: 'Example',
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-01T09:00:00Z',
  url_count: 6,
  sites: [
    {
      site_id: 's1',
      name: 'Example',
      role: 'client',
      main: true,
      domains: ['example.com'],
      note: '',
      tags: [],
      url_count: 3,
      last_run: null,
      namespace: 'n1',
    },
    {
      site_id: 's2',
      name: 'Rival',
      role: 'competitor',
      main: false,
      domains: ['rival.com'],
      note: '',
      tags: [],
      url_count: 3,
      last_run: null,
      namespace: 'n2',
    },
  ],
  last_run: null,
};

const snapshot: ResearchSnapshot = {
  snapshot_id: 'research-20261102-101010-001',
  project_id: 'p1',
  created_at: '2026-11-02T10:10:10Z',
  database: 'us',
  websites: [
    {
      website: 'example.com',
      name: 'Example',
      role: 'client',
      source: 'csv_import',
      pulled_at: '2026-11-02T10:10:10Z',
      overview: null,
      keywords: [
        {
          keyword: 'beneficiary change form',
          position: 14,
          previous_position: 18,
          volume: 900,
          cpc: 1,
          competition: null,
          difficulty: null,
          url: 'https://example.com/forms',
          traffic_share: 0,
          theme_id: 'th-1',
        },
      ],
      note: '',
    },
  ],
  keywords: [],
  theme_map_id: 'tm-1',
  demand: [
    { theme_id: 'th-2', label: 'Pet insurance', keywords: 3, volume: 6600, top10: { 'rival.com': 2 }, ranked: {}, traffic: {} },
  ],
  units_spent: 0,
  notes: [],
};

const offStatus: ResearchStatus = {
  enabled: false,
  api_key_set: false,
  monthly_cap: 0,
  used_this_month: 0,
  pull_cap: 5000,
  keyword_limit: 200,
  database: 'us',
};

const report: GapReport = {
  report_id: 'gaps-20261102-101010-001',
  project_id: 'p1',
  created_at: '2026-11-02T10:10:10Z',
  theme_map_id: 'tm-1',
  research_snapshot_id: null,
  monitor_check_id: null,
  missing_inputs: ['No research yet: keyword gaps were skipped.'],
  findings: [
    {
      finding_id: 'gap-aaaaaaaaaaaa',
      kind: 'theme_gap',
      severity: 'high',
      priority: 74,
      title: 'No client content on Pet insurance',
      detail: 'Rival has 3 pages on this theme; the client has none.',
      action: 'Plan a pillar page on Pet insurance.',
      theme_id: 'th-2',
      theme: 'Pet insurance',
      competitors: ['rival.com'],
      demand: 6600,
      evidence: [{ kind: 'pillar', label: 'Pet cover guide', website: 'rival.com', url: 'https://rival.com/pet', value: null }],
      status: 'open',
      note: '',
    },
    {
      finding_id: 'gap-bbbbbbbbbbbb',
      kind: 'funnel_gap',
      severity: 'low',
      priority: 30,
      title: 'No compare page in Term life',
      detail: 'Competitors cover the compare stage.',
      action: 'Write a comparison page.',
      theme_id: 'th-1',
      theme: 'Term life',
      competitors: ['rival.com'],
      demand: 0,
      evidence: [],
      status: 'dismissed',
      note: 'not now',
    },
  ],
  resolved: ['Missing offering: Annuities'],
  stats: { findings: 2, high: 1, kind_theme_gap: 1, kind_funnel_gap: 1 },
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
};

const FIRST_PAINT = { timeout: 15_000 };
const TIMEOUT = 120_000;

beforeEach(() => useProjectTokens.setState({ tokens: {} }));
afterEach(() => vi.unstubAllGlobals());

function suggestion(domain: string, extra: Partial<CompetitorSuggestion> = {}): CompetitorSuggestion {
  return {
    domain,
    status: 'suggested',
    sources: ['semrush_competitors'],
    relevance: 0.44,
    common_keywords: 95,
    organic_keywords: 12000,
    organic_traffic: 150000,
    search_results: 0,
    example_keywords: [],
    first_seen: '2026-11-04T09:00:00Z',
    last_seen: '2026-11-04T09:00:00Z',
    times_seen: 1,
    decided_at: null,
    site_id: null,
    ...extra,
  };
}

const book: CompetitorBook = {
  project_id: 'p1',
  updated_at: '2026-11-04T09:00:00Z',
  client_domain: 'example.com',
  suggestions: [
    suggestion('bigsure.com'),
    suggestion('newco.io', { sources: ['search_results'], relevance: null, common_keywords: 0, search_results: 2 }),
    suggestion('rival.com', { status: 'added', site_id: 's2' }),
  ],
};

describe('suggested competitors', () => {
  it(
    'lists what the search data found; adds one to the project and dismisses another once unlocked',
    async () => {
      let current = book;
      const decide = (domain: string, status: CompetitorSuggestion['status']) => {
        current = {
          ...current,
          suggestions: current.suggestions.map((s) => (s.domain === domain ? { ...s, status } : s)),
        };
        return [200, current] as [number, unknown];
      };
      const sent = fakeServer({
        ...base,
        'GET /api/research/status': () => [200, offStatus],
        'GET /api/projects/p1/research': () => [404, { detail: 'No research has been added yet.' }],
        'GET /api/projects/p1/research/competitors': () => [200, current],
        'POST /api/projects/p1/research/competitors/bigsure.com/add': () => decide('bigsure.com', 'added'),
        'POST /api/projects/p1/research/competitors/newco.io/dismiss': () => decide('newco.io', 'dismissed'),
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects/p1?tab=research');
      expect(await screen.findByText('bigsure.com', {}, FIRST_PAINT)).toBeInTheDocument();
      const row = (d: string) => screen.getByText(d).closest('tr') as HTMLElement;
      expect(within(row('bigsure.com')).getByText('44%')).toBeInTheDocument();
      expect(within(row('bigsure.com')).getByText((150000).toLocaleString())).toBeInTheDocument();
      expect(within(row('newco.io')).getByText('2 keywords')).toBeInTheDocument();
      expect(within(row('rival.com')).getByText('In the project')).toBeInTheDocument();
      expect(within(row('bigsure.com')).getByText('Add as competitor').closest('button')).toBeDisabled();
      act(() => useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z'));
      await waitFor(() =>
        expect(within(row('bigsure.com')).getByText('Add as competitor').closest('button')).not.toBeDisabled(),
      );
      await user.click(within(row('bigsure.com')).getByText('Add as competitor').closest('button') as HTMLElement);
      await waitFor(() => expect(within(row('bigsure.com')).getByText('In the project')).toBeInTheDocument());
      expect(sent[0]?.path).toBe('/api/projects/p1/research/competitors/bigsure.com/add');
      expect(sent[0]?.headers['X-Project-Token']).toBe('tok-1');
      await user.click(within(row('newco.io')).getByText('Dismiss').closest('button') as HTMLElement);
      await waitFor(() => expect(screen.queryByText('newco.io')).not.toBeInTheDocument());
      expect(screen.getByText('Show dismissed (1)')).toBeInTheDocument();
    },
    TIMEOUT,
  );
});

describe('research tab', () => {
  it(
    'imports a Semrush export once unlocked and shows rankings and demand; pulls stay off',
    async () => {
      let imported = false;
      const sent = fakeServer({
        ...base,
        'GET /api/research/status': () => [200, offStatus],
        'GET /api/projects/p1/research': () =>
          imported ? [200, snapshot] : [404, { detail: 'No research has been added yet.' }],
        'GET /api/projects/p1/research/movers': () => [404, { detail: 'Add research again later to compare.' }],
        'GET /api/projects/p1/research/competitors': () => [200, { ...book, suggestions: [] }],
        'POST /api/projects/p1/research/import': () => {
          imported = true;
          return [200, snapshot];
        },
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      const { container } = renderApp('/projects/p1?tab=research');
      expect(await screen.findByText('No search data has been added to this project yet.', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(screen.getByText(/no API key is set/)).toBeInTheDocument();
      expect(screen.getByText('Choose the CSV file').closest('button')).toBeDisabled();
      act(() => useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z'));
      await waitFor(() => expect(screen.getByText('Choose the CSV file').closest('button')).not.toBeDisabled());
      const input = container.querySelector('input[type="file"]') as HTMLInputElement;
      const file = new File(['Keyword,Position\nbeneficiary change form,14\n'], 'positions.csv', { type: 'text/csv' });
      fireEvent.change(input, { target: { files: [file] } });
      expect(await screen.findByText('Pet insurance')).toBeInTheDocument();
      expect(sent[0]?.headers['X-Project-Token']).toBe('tok-1');
      const body = sent[0]?.body as { website: string; file_name: string; content_base64: string };
      expect(body.website).toBe('example.com');
      expect(body.file_name).toBe('positions.csv');
      expect(atob(body.content_base64)).toContain('beneficiary change form,14');
      expect(screen.getByText('rival.com 2')).toBeInTheDocument();
      expect(await screen.findByText('Add research again later to see what moved.')).toBeInTheDocument();
      const row = screen.getByText('Imported export').closest('tr') as HTMLElement;
      await user.click(within(row).getByRole('button'));
      expect(await screen.findByText('beneficiary change form')).toBeInTheDocument();
    },
    TIMEOUT,
  );

  it(
    'shows the unit budget and estimate when Semrush is switched on, and sends the keywords',
    async () => {
      const sent = fakeServer({
        ...base,
        'GET /api/research/status': () => [200, { ...offStatus, enabled: true, api_key_set: true, monthly_cap: 20000, used_this_month: 120 }],
        'GET /api/projects/p1/research': () => [200, snapshot],
        'GET /api/projects/p1/research/movers': () => [
          200,
          [{ website: 'example.com', keyword: 'beneficiary change form', before: 18, after: 14, volume: 900, url: null }],
        ],
        'GET /api/projects/p1/research/estimate': () => [200, { websites: 2, keyword_limit: 200, worst_case_units: 4020, pull_cap: 5000 }],
        'POST /api/projects/p1/research/pull': () => [200, { ...snapshot, units_spent: 310 }],
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      act(() => useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z'));
      renderApp('/projects/p1?tab=research');
      expect(await screen.findByText('Units used this month: 120 of 20000', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(await screen.findByText(/At most 4020 units for 2 websites/)).toBeInTheDocument();
      expect(await screen.findByText('18 → 14')).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText('Keywords to look up too (optional, one per line)'), {
        target: { value: 'pet insurance\n' },
      });
      await user.click(screen.getByText('Pull from Semrush').closest('button') as HTMLElement);
      await waitFor(() => expect(sent).toHaveLength(1));
      expect(sent[0]?.body).toEqual({ keywords: ['pet insurance'] });
    },
    TIMEOUT,
  );
});

describe('gaps tab', () => {
  it(
    'builds the analysis once unlocked, filters findings and records a decision',
    async () => {
      let built = false;
      const sent = fakeServer({
        ...base,
        'GET /api/projects/p1/gaps': () => (built ? [200, report] : [404, { detail: 'No gap analysis has been built yet.' }]),
        'POST /api/projects/p1/gaps': () => {
          built = true;
          return [200, report];
        },
        'POST /api/projects/p1/gaps/findings/gap-aaaaaaaaaaaa/status': (body) => [
          200,
          { ...(body as object), updated_at: '2026-11-02T11:00:00Z' },
        ],
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects/p1?tab=gaps');
      expect(await screen.findByText('No gap analysis has been built for this project yet.', {}, FIRST_PAINT)).toBeInTheDocument();
      const button = () => screen.getByText('Build the gap analysis').closest('button') as HTMLElement;
      expect(button()).toBeDisabled();
      act(() => useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z'));
      await waitFor(() => expect(button()).not.toBeDisabled());
      await user.click(button());
      expect(await screen.findByText('No client content on Pet insurance')).toBeInTheDocument();
      expect(sent[0]?.headers['X-Project-Token']).toBe('tok-1');
      expect(screen.getByText('Plan a pillar page on Pet insurance.')).toBeInTheDocument();
      expect(screen.getByText(/6,600 searches \/ month/)).toBeInTheDocument();
      expect(screen.getByText('Pet cover guide')).toHaveAttribute('href', 'https://rival.com/pet');
      expect(screen.getByText('No research yet: keyword gaps were skipped.')).toBeInTheDocument();
      expect(screen.getByText('Missing offering: Annuities')).toBeInTheDocument();
      expect(screen.queryByText('No compare page in Term life')).not.toBeInTheDocument();
      await user.click(screen.getByText('Hide dismissed'));
      expect(await screen.findByText('No compare page in Term life')).toBeInTheDocument();
      const select = screen.getByRole('combobox', { name: 'Status: No client content on Pet insurance' });
      await user.click(select);
      await user.click(await screen.findByTitle('Accepted'));
      await waitFor(() => expect(sent).toHaveLength(2));
      expect(sent[1]?.body).toEqual({ status: 'accepted', note: '' });
    },
    TIMEOUT,
  );
});

describe('gap settings', () => {
  it(
    'shows how keyword gaps are counted and saves a change once unlocked',
    async () => {
      const sent = fakeServer({
        ...base,
        'GET /api/projects/p1/gaps': () => [200, { ...report, settings: { min_volume: 0, competitor_top: 10 } }],
        'GET /api/projects/p1/gaps/settings': () => [200, { min_volume: 0, competitor_top: 10 }],
        'POST /api/projects/p1/gaps/settings': (body) => [200, body],
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects/p1?tab=gaps');
      expect(await screen.findByText('How keyword gaps are counted', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(await screen.findByText(/Counted: keywords with 0\+ searches/)).toBeInTheDocument();
      const card = screen.getByText('How keyword gaps are counted').closest('.ant-card') as HTMLElement;
      const save = () => within(card).getByText('Save').closest('button') as HTMLElement;
      expect(save()).toBeDisabled();
      act(() => useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z'));
      await waitFor(() => expect(save()).not.toBeDisabled());
      const volume = within(card).getByLabelText('Minimum searches a month');
      await user.clear(volume);
      await user.type(volume, '500');
      await user.click(save());
      await waitFor(() => expect(sent).toHaveLength(1));
      expect(sent[0]?.path).toBe('/api/projects/p1/gaps/settings');
      expect(sent[0]?.body).toEqual({ min_volume: 500, competitor_top: 10 });
      expect(sent[0]?.headers['X-Project-Token']).toBe('tok-1');
    },
    TIMEOUT,
  );
});
