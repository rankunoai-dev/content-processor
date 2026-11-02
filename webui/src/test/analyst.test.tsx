import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ActionCard, CardRun, CardSummary } from '../api/analyst';
import { useProjectTokens, type Project, type Site } from '../api/projects';
import { renderApp } from './utils';

const site = (site_id: string, name: string, role: Site['role'], domain: string): Site => ({
  site_id,
  name,
  role,
  domains: [domain, `www.${domain}`],
  url_count: 3,
  last_run: null,
  namespace: `rankuno-dev.p1.${site_id}`,
  main: role === 'client',
  note: '',
  tags: [],
});

const project: Project = {
  project_id: 'p1',
  name: 'Print security',
  owner_name: 'Gaurav',
  client_name: "Flynn's",
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-01T09:00:00Z',
  url_count: 6,
  sites: [site('flynn-s', "Flynn's", 'client', 'flynns.com'), site('xerox', 'Xerox', 'competitor', 'xerox.com')],
  last_run: null,
};

const cards: CardSummary[] = [
  {
    id: 'eeat-trust-audit',
    title: 'E-E-A-T & Trust Proof Audit',
    module: 'content',
    category: 'Content quality',
    icon: 'shield-check',
    summary: 'Checks how well the client proves trust.',
    deliverable: 'Trust & Credibility Matrix',
    available: true,
    needs_modules: [],
  },
  {
    id: 'serp-intent-opportunity',
    title: 'Search Intent & SERP Feature Opportunity',
    module: 'research',
    category: 'Keyword research',
    icon: 'search',
    summary: 'Reads who ranks for a keyword.',
    deliverable: 'SERP Win Opportunity Score',
    available: false,
    needs_modules: ['research'],
  },
];

const eeat: ActionCard = {
  id: 'eeat-trust-audit',
  version: 1,
  title: 'E-E-A-T & Trust Proof Audit',
  module: 'content',
  category: 'Content quality',
  summary: 'Checks how well the client proves trust.',
  deliverable: 'Trust & Credibility Matrix',
  parameters: [
    { name: 'project', label: 'Project', kind: 'project', description: '', required: true, default: null, choices: [], many: false },
    { name: 'client', label: 'Client website', kind: 'website', description: '', required: true, default: 'client', choices: [], many: false },
    {
      name: 'competitors',
      label: 'Competitor websites',
      kind: 'website',
      description: '',
      required: true,
      default: 'competitors',
      choices: [],
      many: true,
    },
    {
      name: 'topic',
      label: 'Topic or offering',
      kind: 'topic',
      description: 'Narrow it',
      required: false,
      default: "the website's main offerings",
      choices: [],
      many: false,
    },
  ],
  required_tools: ['search_knowledge'],
  optional_tools: [],
  output_format: '# Matrix',
};

const run = (over: Partial<CardRun>): CardRun => ({
  run_id: 'run-aaaaaaaaaaaa',
  card_id: 'eeat-trust-audit',
  card_version: 1,
  backend: 'pack',
  status: 'finished',
  created_at: '2026-11-02T10:00:00Z',
  finished_at: '2026-11-02T10:00:05Z',
  inputs: {},
  project_ids: ['p1'],
  model: null,
  tool_calls: 2,
  turns: 0,
  usage: {},
  cost_usd: 0,
  cap_usd: null,
  message: 'context pack written',
  verification: null,
  files: { context_pack: 'context_pack.md' },
  ...over,
});

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

function buttonIn(scope: Pick<typeof screen, 'getByText'>, text: string | RegExp): HTMLElement {
  const button = scope.getByText(text).closest('button');
  if (!button) throw new Error(`no button showing ${String(text)}`);
  return button;
}

async function drawer(title: string): Promise<HTMLElement> {
  return waitFor(
    () => {
      const found = [...document.querySelectorAll<HTMLElement>('.ant-drawer-content')].find((d) =>
        d.textContent?.includes(title),
      );
      if (!found) throw new Error('no drawer yet');
      return found;
    },
    { timeout: 15_000 },
  );
}

const routes = (runs: CardRun[] = []) => ({
  [`GET /api/projects/p1`]: () => [200, project] as [number, unknown],
  [`GET /api/projects/p1/runs`]: () => [200, []] as [number, unknown],
  'GET /api/analyst/cards': () => [200, cards] as [number, unknown],
  'GET /api/analyst/cards/eeat-trust-audit': () => [200, eeat] as [number, unknown],
  'GET /api/projects/p1/analyst/runs': () => [200, runs] as [number, unknown],
});

const FIRST_PAINT = { timeout: 15_000 };
const TIMEOUT = 120_000;

beforeEach(() => useProjectTokens.setState({ tokens: {} }));
afterEach(() => vi.unstubAllGlobals());

describe('analyst cards', () => {
  it(
    'shows the library by module, with unavailable cards and why',
    async () => {
      fakeServer(routes());
      renderApp('/projects/p1?tab=analyst');
      expect(await screen.findByText('E-E-A-T & Trust Proof Audit', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(screen.getByText('Trust & Credibility Matrix')).toBeInTheDocument();
      expect(screen.getByText('Keyword research')).toBeInTheDocument();
      expect(screen.getAllByText(/Needs the Research \(Semrush\) module/).length).toBeGreaterThan(0);
      expect(screen.getByText('No card has been run for this project yet.')).toBeInTheDocument();
    },
    TIMEOUT,
  );

  it(
    'checks a card with project defaults, refuses to run while locked, runs when unlocked',
    async () => {
      const sent = fakeServer({
        ...routes(),
        'POST /api/projects/p1/analyst/preflight': () => [
          200,
          {
            card_id: 'eeat-trust-audit',
            runnable: true,
            inputs: {},
            problems: [],
            warnings: ["Xerox is searched by shared words only (offline embedder)"],
            tools: [],
            missing_tools: [],
          },
        ],
        'POST /api/projects/p1/analyst/runs': () => [200, run({ status: 'queued' })],
        'GET /api/projects/p1/analyst/runs/run-aaaaaaaaaaaa': () => [
          200,
          {
            record: run({}),
            text: '# Context pack: E-E-A-T\n\nThe brief and the data.',
            events: [
              { at: '2026-11-02T10:00:01Z', type: 'prefetch', tool: 'search_knowledge', error: false },
              { at: '2026-11-02T10:00:05Z', type: 'end', status: 'finished', message: 'context pack written' },
            ],
          },
        ],
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects/p1?tab=analyst');
      await screen.findByText('E-E-A-T & Trust Proof Audit', {}, FIRST_PAINT);
      const use = screen.getAllByText('Use this card')[0]?.closest('button');
      if (!use) throw new Error('no Use this card button');
      await user.click(use);
      const form = await drawer('Inputs');
      expect(within(form).getByText('The client’s website')).toBeInTheDocument();
      expect(within(form).getByText('All competitor websites of the project')).toBeInTheDocument();
      await user.click(buttonIn(within(form), 'Check'));
      expect(await within(form).findByText('Ready to run.')).toBeInTheDocument();
      expect(within(form).getByText(/shared words only/)).toBeInTheDocument();
      expect(sent[0]?.body).toEqual({ card: 'eeat-trust-audit', inputs: {}, backend: 'pack' });
      expect(buttonIn(within(form), /^Run$/)).toBeDisabled();
      expect(within(form).getByText('Unlock the project with its password to run cards.')).toBeInTheDocument();

      act(() => useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z'));
      await waitFor(() => expect(buttonIn(within(form), /^Run$/)).not.toBeDisabled());
      await user.click(buttonIn(within(form), /^Run$/));
      await waitFor(() => expect(sent).toHaveLength(2));
      expect(sent[1]?.path).toBe('/api/projects/p1/analyst/runs');
      expect(sent[1]?.headers['X-Project-Token']).toBe('tok-1');
      const viewer = await drawer('Card run:');
      expect(await within(viewer).findByText(/The brief and the data/)).toBeInTheDocument();
      expect(within(viewer).getByText('Gathered data: search knowledge')).toBeInTheDocument();
      expect(within(viewer).getByText('Download (.md)')).toBeInTheDocument();
    },
    TIMEOUT,
  );

  it(
    'lists runs and shows a Claude run whose citations need review',
    async () => {
      const claudeRun = run({
        run_id: 'run-bbbbbbbbbbbb',
        backend: 'claude',
        model: 'claude-opus-5-5',
        turns: 3,
        tool_calls: 4,
        cost_usd: 0.4123,
        cap_usd: 2,
        message: 'finished; the verification needs review',
        verification: {
          references: 10,
          resolved: 9,
          unknown_refs: ['chunk:nope'],
          quotes_checked: 4,
          quotes_failed: [],
          uncited_lines: ['A claim with 12 numbers'],
          passed: false,
        },
        files: { deliverable: 'deliverable.md' },
      });
      fakeServer({
        ...routes([claudeRun]),
        'GET /api/projects/p1/analyst/runs/run-bbbbbbbbbbbb': () => [
          200,
          { record: claudeRun, text: '# Trust & Credibility Matrix\n\n## Sources', events: [] },
        ],
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects/p1?tab=analyst');
      expect(await screen.findByText('$0.4123', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(screen.getByText('Needs review')).toBeInTheDocument();
      await user.click(buttonIn(screen, 'Open'));
      const viewer = await drawer('Card run:');
      expect(within(viewer).getByText('$0.4123 of a $2.00 cap')).toBeInTheDocument();
      expect(within(viewer).getByText(/9 of 10 citations point at data from this run/)).toBeInTheDocument();
      expect(within(viewer).getByText(/claude-opus-5-5/)).toBeInTheDocument();
      expect(within(viewer).getByText(/## Sources/)).toBeInTheDocument();
    },
    TIMEOUT,
  );
});
