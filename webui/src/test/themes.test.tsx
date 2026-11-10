import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectTokens, type Project } from '../api/projects';
import type { ThemeMap } from '../api/themes';
import { renderApp } from './utils';

const project: Project = {
  project_id: 'p1',
  name: 'Print security',
  owner_name: 'Gaurav',
  confidential: false,
  client_name: "Flynn's",
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-01T09:00:00Z',
  url_count: 6,
  sites: [],
  last_run: null,
};

const map: ThemeMap = {
  map_id: 'themes-20261102-101010-001',
  project_id: 'p1',
  built_at: '2026-11-02T10:10:10Z',
  method: 'TF-IDF',
  websites: [
    { website: 'flynns.com', name: "Flynn's", role: 'client', project: 'Print security', run_id: 'r1', pages: 8 },
    { website: 'xerox.com', name: 'Xerox', role: 'competitor', project: 'Print security', run_id: 'r2', pages: 6 },
  ],
  themes: [
    {
      theme_id: 'th-1',
      label: 'Secure print, release',
      terms: ['secure print', 'release', 'badge'],
      pages: 5,
      coverage: [
        { website: 'flynns.com', pages: 1, words: 400, share: 0.2, pillar: null },
        {
          website: 'xerox.com',
          pages: 4,
          words: 3000,
          share: 0.8,
          pillar: { url: 'https://www.xerox.com/secure', title: 'Xerox secure print', score: 0.8, reasons: ['centrality 0.90'] },
        },
      ],
      ownership: 'competitor_led',
      ownership_reason: "a competitor has 4 page(s) vs the client's 1",
      topics: [{ topic_id: 'tp-1', label: 'Badge release', terms: ['badge'], pages: ['a', 'b'], websites: { 'xerox.com': 2 } }],
      funnel: { learn: 3, decide: 2 },
      offerings: ['Managed print'],
    },
    {
      theme_id: 'th-2',
      label: 'Toner, cartridges',
      terms: ['toner'],
      pages: 3,
      coverage: [
        { website: 'flynns.com', pages: 3, words: 900, share: 1, pillar: null },
        { website: 'xerox.com', pages: 0, words: 0, share: 0, pillar: null },
      ],
      ownership: 'client_only',
      ownership_reason: 'only the client covers it (3 page(s))',
      topics: [],
      funnel: { decide: 3 },
      offerings: [],
    },
  ],
  pages: [
    { url: 'https://www.flynns.com/a', website: 'flynns.com', title: 'A', theme_id: 'th-2', topic_id: null, role: 'spoke', similarity: 0.5, funnel_stage: 'decide', funnel_reason: 'x', page_type: null, words: 300 },
    { url: 'https://www.xerox.com/b', website: 'xerox.com', title: 'B', theme_id: 'th-1', topic_id: null, role: 'pillar', similarity: 0.9, funnel_stage: 'learn', funnel_reason: 'x', page_type: null, words: 800 },
  ],
  skipped: [],
  stats: { themes: 2, topics: 1, pages: 14, unclustered: 1 },
};

interface Sent {
  path: string;
  headers: Record<string, string>;
}

function fakeServer(routes: Record<string, () => [number, unknown]>) {
  const sent: Sent[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1');
      const method = init?.method ?? 'GET';
      if (method === 'POST') sent.push({ path: url.pathname, headers: (init?.headers ?? {}) as Record<string, string> });
      const handler = routes[`${method} ${url.pathname}`];
      const [status, payload] = handler ? handler() : [404, { detail: 'There is no such thing.' }];
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

describe('themes tab', () => {
  it(
    'offers to build a map, only when unlocked, and shows it once built',
    async () => {
      let built = false;
      const sent = fakeServer({
        ...base,
        'GET /api/projects/p1/themes': () =>
          built ? [200, map] : [404, { detail: 'No theme map has been built yet.' }],
        'POST /api/projects/p1/themes': () => {
          built = true;
          return [200, map];
        },
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects/p1?tab=themes');
      expect(await screen.findByText('No theme map has been built for this project yet.', {}, FIRST_PAINT)).toBeInTheDocument();
      const button = () => screen.getByText('Build the theme map').closest('button') as HTMLElement;
      expect(button()).toBeDisabled();
      act(() => useProjectTokens.getState().remember('p1', 'tok-1', '2099-01-01T00:00:00Z'));
      await waitFor(() => expect(button()).not.toBeDisabled());
      await user.click(button());
      expect(await screen.findByText('Secure print, release')).toBeInTheDocument();
      expect(sent[0]?.headers['X-Project-Token']).toBe('tok-1');
      expect(screen.getByText('Build again')).toBeInTheDocument();
      await user.click(screen.getByText('Download').closest('button') as HTMLElement);
      expect((await screen.findByText('Excel (.xlsx)')).closest('a')).toHaveAttribute(
        'href',
        '/api/projects/p1/exports/themes.xlsx',
      );
      expect(screen.getByText('CSV').closest('a')).toHaveAttribute('href', '/api/projects/p1/exports/themes.csv');
      expect(screen.getByText('Word (.docx)').closest('a')).toHaveAttribute('href', '/api/projects/p1/exports/themes.docx');
    },
    TIMEOUT,
  );

  it(
    'filters by ownership and shows a theme’s pillars, topics and funnel',
    async () => {
      fakeServer({ ...base, 'GET /api/projects/p1/themes': () => [200, map] });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects/p1?tab=themes');
      expect(await screen.findByText('Toner, cartridges', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(screen.getByText(/2 themes · 1 topics · 14 pages/)).toBeInTheDocument();
      expect(screen.getAllByText('Competitor leads').length).toBeGreaterThan(0);
      await user.click(screen.getByText('Competitor leads (1)'));
      await waitFor(() => expect(screen.queryByText('Toner, cartridges')).not.toBeInTheDocument());
      const row = screen.getByText('Secure print, release').closest('tr') as HTMLElement;
      await user.click(within(row).getByRole('button'));
      expect(await screen.findByText('Xerox secure print')).toBeInTheDocument();
      expect(screen.getByText(/Badge release/)).toBeInTheDocument();
      expect(screen.getByText('Learn 3 · Decide 2')).toBeInTheDocument();
      expect(screen.getByText('Managed print')).toBeInTheDocument();
      expect(screen.getByText('Funnel stages per website')).toBeInTheDocument();
    },
    TIMEOUT,
  );
});
