import { screen, waitFor } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectTokens, type Project, type ProjectReport, type ProjectReports } from '../api/projects';
import { renderApp } from './utils';

const project: Project = {
  project_id: 'ins-1',
  name: 'Insurer audit',
  owner_name: 'G',
  client_name: 'Insurer',
  created_at: '2026-11-03T09:00:00Z',
  updated_at: '2026-11-03T09:00:00Z',
  url_count: 9,
  sites: [
    { site_id: 'insurer', name: 'Insurer', role: 'client', main: true, domains: ['insurer.com'], note: '', tags: [], url_count: 7, last_run: null, namespace: 'n.insurer' },
    { site_id: 'direct', name: 'Insurer Direct', role: 'client', main: false, domains: ['direct-insurer.com'], note: '', tags: [], url_count: 1, last_run: null, namespace: 'n.direct' },
    { site_id: 'rival', name: 'Rival', role: 'competitor', main: false, domains: ['rival.com'], note: '', tags: [], url_count: 1, last_run: null, namespace: 'n.rival' },
  ],
  last_run: null,
};

const sources: ProjectReports['sources'] = [
  { site_id: 'insurer', site_name: 'Insurer', role: 'client', main: true, run_id: 'run-1', finished_at: '2026-11-03T10:00:00Z', pages: 7, reason: '' },
  { site_id: 'direct', site_name: 'Insurer Direct', role: 'client', main: false, run_id: null, finished_at: null, pages: 0, reason: 'No finished scrape yet.' },
  { site_id: 'rival', site_name: 'Rival', role: 'competitor', main: false, run_id: 'run-3', finished_at: '2026-11-03T10:00:00Z', pages: 2, reason: '' },
];

const built: ProjectReport = {
  stamp: '20261103-110000',
  built_at: '2026-11-03T11:00:00Z',
  include_competitors: true,
  pages: 7,
  contradictions: 3,
  contradictions_high: 2,
  differs_by_condition: 1,
  time_bound: 4,
  duplicate_pairs: 5,
  duplicate_pairs_across_websites: 1,
  websites: [sources[0]!],
  files: [
    { name: 'Insurer_audit_duplication.xlsx', size: 2048, modified_at: '2026-11-03T11:00:00Z' },
    { name: 'Insurer_audit_project_report.xlsx', size: 8192, modified_at: '2026-11-03T11:00:00Z' },
    { name: 'Insurer_audit_contradictions_by_topic.xlsx', size: 4096, modified_at: '2026-11-03T11:00:00Z' },
  ],
};

function fakeServer(reports: { current: ProjectReports }) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1');
      const method = init?.method ?? 'GET';
      const body: unknown = init?.body ? JSON.parse(String(init.body)) : undefined;
      let payload: unknown = { detail: 'There is no such thing.' };
      let status = 404;
      if (method === 'GET' && url.pathname === '/api/projects/ins-1') [status, payload] = [200, project];
      if (method === 'GET' && url.pathname === '/api/projects/ins-1/runs') [status, payload] = [200, []];
      if (url.pathname === '/api/projects/ins-1/reports') {
        if (method === 'POST') {
          sent.push({ path: url.pathname, body });
          // Building runs in the background: the answer says so, the next look shows the report.
          [status, payload] = [200, { ...reports.current, building: true, building_since: '2026-11-04T09:00:00Z' }];
          reports.current = { sources, reports: [built], building: false, building_since: null, last_error: '' };
          return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
        }
        [status, payload] = [200, reports.current];
      }
      return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
    }),
  );
  return sent;
}

const FIRST_PAINT = { timeout: 15_000 };

beforeEach(() => {
  useProjectTokens.setState({ tokens: {} });
});
afterEach(() => vi.unstubAllGlobals());

describe('project reports', () => {
  it('lists the websites used, builds the report and offers the downloads', async () => {
    const sent = fakeServer({ current: { sources, reports: [], building: false, building_since: null, last_error: '' } });
    useProjectTokens.getState().remember('ins-1', 'tok', '2099-01-01T00:00:00Z');
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderApp('/projects/ins-1?tab=project-reports');

    expect(await screen.findByText('Websites this report reads', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(screen.getByText('Main website')).toBeInTheDocument();
    expect(screen.getByText('No finished scrape yet.')).toBeInTheDocument();
    expect(screen.getByText(/No project report yet/)).toBeInTheDocument();

    await user.click(screen.getByText('Include competitors (separate workbook)'));
    await user.click(screen.getByText('Build project report').closest('button') as HTMLElement);
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({ include_competitors: true });

    expect(await screen.findByText(/Large websites take a few minutes/)).toBeInTheDocument();
    expect(await screen.findByText('Latest report', {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText('Topics stated differently')).toBeInTheDocument();
    expect(screen.getByText('2 of them High severity')).toBeInTheDocument();
    expect(screen.getByText('1 across websites')).toBeInTheDocument();
    const rows = screen.getAllByText(/\(Excel\)/).map((n) => n.textContent ?? '');
    expect(rows[0]).toMatch(/^Contradictions by topic/);
    expect(rows[1]).toMatch(/^Project report \(Excel\): every page with its whole content/);
    expect(rows[2]).toMatch(/^Duplication/);
    const zip = screen.getByText('Download all (zip)').closest('a');
    expect(zip?.getAttribute('href')).toBe('/api/projects/ins-1/reports/20261103-110000/downloads.zip');
  }, 120_000);
});
