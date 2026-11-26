import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { App as AntApp } from 'antd';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { BacklinkSnapshot, BacklinksStatus } from '../api/backlinks';
import type { Project } from '../api/projects';
import { BacklinksCard } from '../routes/BacklinksCard';

const site = (site_id: string, name: string, role: 'client' | 'competitor', domain: string) => ({
  site_id,
  name,
  role,
  main: role === 'client',
  domains: [domain],
  note: '',
  tags: [],
  url_count: 3,
  last_run: null,
  namespace: site_id,
});

const project: Project = {
  project_id: 'p1',
  name: 'Life cover',
  owner_name: 'Gaurav',
  confidential: false,
  client_name: 'Example',
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-01T09:00:00Z',
  url_count: 6,
  sites: [site('s1', 'Example', 'client', 'example.com'), site('s2', 'Rival', 'competitor', 'rival.com')],
  last_run: null,
};

const website = (name: string, key: string, role: 'client' | 'competitor', dr: number) => ({
  website: key,
  name,
  role,
  source: 'ahrefs_api' as const,
  pulled_at: '2026-11-26T10:00:00Z',
  domain_rating: dr,
  ahrefs_rank: 5000,
  stats: { live: 900, all_time: 1400, live_refdomains: 120, all_time_refdomains: 160 },
  refdomains: [],
  refdomains_asked: 500,
  top_pages: [],
  lost_domains: [],
  new_domains: null,
  note: '',
});

const snapshot: BacklinkSnapshot = {
  snapshot_id: 'backlinks-20261126-100000-000',
  project_id: 'p1',
  created_at: '2026-11-26T10:00:00Z',
  websites: [website('Example', 'example.com', 'client', 35), website('Rival', 'rival.com', 'competitor', 62)],
  insights: {
    client: 'example.com',
    min_competitors: 1,
    link_intersect: [{ domain: 'bignews.com', domain_rating: 88, competitors: ['rival.com'], links: 14 }],
    authority: [
      {
        competitor: 'rival.com',
        metric: 'domain_rating',
        client: 35,
        competitor_value: 62,
        gap: 27,
        client_refdomains: 120,
        competitor_refdomains: 300,
      },
    ],
    client_top_pages: [
      { url: 'https://www.example.com/tools/calculator', title: 'Calculator', referring_domains: 55, links: 90, url_rating: 30, http_code: 200 },
    ],
    lost: {},
    notes: [],
  },
  units_spent: 9200,
  notes: ['DUMMY DATA from the Ahrefs stand-in: invented numbers for trying the module, not Ahrefs figures'],
};

const status: BacklinksStatus = {
  enabled: true,
  api_key_set: true,
  monthly_cap: 50_000,
  used_this_month: 9_200,
  pull_cap: 10_000,
  refdomains_limit: 500,
  pages_limit: 100,
  min_interval_ms: 500,
};

function serve(routes: Record<string, () => [number, unknown]>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1');
      const handler = routes[`${init?.method ?? 'GET'} ${url.pathname}`];
      const [code, payload] = handler ? handler() : [404, { detail: 'There is no such thing.' }];
      return new Response(JSON.stringify(payload), { status: code, headers: { 'Content-Type': 'application/json' } });
    }),
  );
}

function show(token: string | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AntApp>
        <BacklinksCard project={project} token={token} />
      </AntApp>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('backlinks card', () => {
  it('shows the websites, the authority gap, the link intersect, the estimate and the workbook', async () => {
    serve({
      'GET /api/backlinks/status': () => [200, status],
      'GET /api/projects/p1/backlinks': () => [200, snapshot],
      'GET /api/projects/p1/backlinks/estimate': () => [
        200,
        { websites: 2, refdomains_limit: 500, pages_limit: 100, worst_case_units: 9200, pull_cap: 10_000 },
      ],
    });
    show('tok-1');
    expect(await screen.findByText('bignews.com', {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.getByText(/Link intersect: 1 website linking to the competitor but not to the client/)).toBeInTheDocument();
    expect(screen.getByText("rival.com: Domain Rating 62 against the client's 35")).toBeInTheDocument();
    expect(screen.getByText(/DUMMY DATA/)).toBeInTheDocument();
    expect(await screen.findByText(/At most 9,200 units for 2 websites/)).toBeInTheDocument();
    const download = screen.getByText('Download the workbook').closest('a') as HTMLAnchorElement;
    expect(download.getAttribute('href')).toBe('/api/projects/p1/backlinks/workbook');
    expect(screen.getByText('https://www.example.com/tools/calculator')).toBeInTheDocument();
    expect(screen.getByText('Pull from Ahrefs').closest('button')).not.toBeDisabled();
  }, 60_000);

  it('says pulls are off and asks for the unlock before importing', async () => {
    serve({
      'GET /api/backlinks/status': () => [200, { ...status, enabled: false, api_key_set: false }],
      'GET /api/projects/p1/backlinks': () => [404, { detail: 'No backlink data has been added yet.' }],
    });
    show(null);
    expect(await screen.findByText(/Ahrefs pulls are switched off: no API key is set/, {}, { timeout: 15_000 })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('No backlink data has been added to this project yet.')).toBeInTheDocument());
    expect(screen.getByText('Choose the export').closest('button')).toBeDisabled();
  }, 60_000);
});
