import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { LiteSummary } from '../api/lite';
import type { Project, Site } from '../api/projects';
import { renderApp } from './utils';

const site = (id: string, role: Site['role']): Site => ({
  site_id: id,
  name: id === 's1' ? 'Example' : 'Rival',
  role,
  main: role === 'client',
  domains: [id === 's1' ? 'example.com' : 'rival.com'],
  note: '',
  tags: [],
  url_count: 3,
  last_run: null,
  namespace: `n-${id}`,
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
  sites: [site('s1', 'client'), site('s2', 'competitor')],
  last_run: null,
};

const summary: LiteSummary = {
  site_id: 's2',
  website: 'rival.com',
  created_at: '2026-11-20T09:00:00Z',
  run_id: 's2_lite',
  inventory: 4980,
  sampled: 150,
  max_pages: 150,
  basis: 'based on 150 of about 4,980 pages (lite scan)',
  sitemap_files: 3,
  sitemap_note: '',
  sections: [{ section: '/blog', pages: 3100, sampled: 40 }],
  sample: [],
  run: null,
};

function serve(lite: LiteSummary | null) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://127.0.0.1').pathname;
      const found: Record<string, unknown> = {
        '/api/projects/p1': project,
        '/api/projects/p1/runs': [],
        '/api/projects/p1/sites/s2/runs': [],
        '/api/queue': { running: [], waiting: [], ready: [], seconds_per_url: 5, email_sends: false, ready_hours: 4 },
      };
      if (lite) found['/api/projects/p1/sites/s2/lite'] = lite;
      const body = path in found ? found[path] : { detail: 'No lite scan of this website yet.' };
      return new Response(JSON.stringify(body), {
        status: path in found ? 200 : 404,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('lite scan card', () => {
  it('shows on a competitor website with what the latest lite scan covered', async () => {
    serve(summary);
    renderApp('/projects/p1?site=s2&view=scrape');
    expect(await screen.findByText('Lite scan (big competitor websites)', {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(
      await screen.findByText(/based on 150 of about 4,980 pages \(lite scan\)/, {}, { timeout: 15_000 }),
    ).toBeInTheDocument();
    expect(screen.getByText('/blog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Run a lite scan/ })).toBeDisabled();
    expect(screen.getByText('Unlock the project to run a lite scan.')).toBeInTheDocument();
  });

  it('is not offered for the client website', async () => {
    serve(null);
    renderApp('/projects/p1?site=s1&view=scrape');
    expect(await screen.findByText(/Start scraping/, {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.queryByText('Lite scan (big competitor websites)')).not.toBeInTheDocument();
  });
});
