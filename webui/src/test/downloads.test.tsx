import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ExportCatalog } from '../api/exports';
import type { Project } from '../api/projects';
import { renderApp } from './utils';

const project: Project = {
  project_id: 'p1',
  name: 'Life cover',
  owner_name: 'Gaurav',
  client_name: 'Example',
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-01T09:00:00Z',
  url_count: 3,
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
  ],
  last_run: null,
};

const catalog: ExportCatalog = {
  formats: [
    { key: 'xlsx', label: 'Excel' },
    { key: 'csv', label: 'CSV' },
    { key: 'docx', label: 'Word' },
  ],
  reports: [
    {
      key: 'overview',
      module: 'content',
      title: 'Project overview',
      description: 'The websites and scrapes.',
      available: true,
      missing: '',
      subtitle: 'Client Example',
      rows: 4,
      formats: ['xlsx', 'csv', 'docx'],
    },
    {
      key: 'gaps',
      module: 'gaps',
      title: 'Gaps',
      description: 'What the client is missing.',
      available: false,
      missing: 'Build the gap analysis in the Gaps tab first.',
      subtitle: '',
      rows: 0,
      formats: ['xlsx', 'csv', 'docx'],
    },
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe('downloads tab', () => {
  it('lists every report with its formats, says what is missing, and offers everything as a zip', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), 'http://127.0.0.1').pathname;
        const body =
          path === '/api/projects/p1'
            ? project
            : path === '/api/projects/p1/runs'
              ? []
              : path === '/api/projects/p1/exports'
                ? catalog
                : { detail: 'There is no such thing.' };
        const status = path.endsWith('/exports') || path === '/api/projects/p1' || path.endsWith('/runs') ? 200 : 404;
        return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
      }),
    );
    renderApp('/projects/p1?tab=downloads');
    expect(await screen.findByText('Project overview', {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.getByText('1 of 2 reports ready.')).toBeInTheDocument();
    expect(screen.getByLabelText('Project overview Word')).toHaveAttribute('href', '/api/projects/p1/exports/overview.docx');
    expect(screen.getByLabelText('Project overview Excel')).toHaveAttribute('href', '/api/projects/p1/exports/overview.xlsx');
    expect(screen.queryByLabelText('Gaps Excel')).not.toBeInTheDocument();
    expect(screen.getByText('Build the gap analysis in the Gaps tab first.')).toBeInTheDocument();
    expect(screen.getByText('Download everything (zip)').closest('a')).toHaveAttribute(
      'href',
      '/api/projects/p1/exports/all.zip',
    );
  }, 60_000);
});
