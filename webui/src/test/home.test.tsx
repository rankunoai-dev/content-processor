import { screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectTokens, type Project } from '../api/projects';
import type { ProjectStatus } from '../api/workspace';
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
  sites: [],
  last_run: null,
};

const other: Project = { ...project, project_id: 'p2', name: 'Print security', client_name: "Flynn's" };

const status = (over: Partial<ProjectStatus>): ProjectStatus => ({
  project_id: 'p1',
  name: 'Life cover',
  client: 'Example',
  websites: [],
  finished_scrapes: 2,
  last_scrape: '2026-11-02T10:00:00Z',
  searchable_scrapes: 2,
  themes_built: '2026-11-02T11:00:00Z',
  themes: 9,
  last_monitoring_check: null,
  alerts: 0,
  high_alerts: 0,
  research_added: null,
  keywords: 0,
  gaps_built: null,
  findings: 0,
  open_findings: 0,
  high_findings: 0,
  ...over,
});

function fakeServer(routes: Record<string, () => [number, unknown]>) {
  const posted: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1');
      const method = init?.method ?? 'GET';
      if (method === 'POST') posted.push({ path: url.pathname, body: JSON.parse(String(init?.body)) });
      const handler = routes[`${method} ${url.pathname}`];
      const [code, payload] = handler ? handler() : [404, { detail: 'There is no such thing.' }];
      return new Response(JSON.stringify(payload), { status: code, headers: { 'Content-Type': 'application/json' } });
    }),
  );
  return posted;
}

const routes = {
  'GET /api/modules/status': () =>
    [200, [status({}), status({ project_id: 'p2', name: 'Print security', themes_built: null, themes: 0 })]] as [
      number,
      unknown,
    ],
  'GET /api/projects': () => [200, [project, other]] as [number, unknown],
  'GET /api/projects/p1': () => [200, project] as [number, unknown],
  'GET /api/projects/p1/runs': () => [200, []] as [number, unknown],
  'GET /api/projects/p1/themes': () => [404, { detail: 'No theme map has been built yet.' }] as [number, unknown],
};

const FIRST_PAINT = { timeout: 15_000 };
const TIMEOUT = 120_000;

beforeEach(() => useProjectTokens.setState({ tokens: {} }));
afterEach(() => vi.unstubAllGlobals());

describe('home: modules as tiles over shared projects', () => {
  it(
    'lists every module, opens one with its projects and opens a project on that module',
    async () => {
      fakeServer(routes);
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/');
      const main = await waitFor(() => {
        const found = document.querySelector<HTMLElement>('.app-content');
        if (!found) throw new Error('no content yet');
        return found;
      }, FIRST_PAINT);
      for (const title of ['Content Scraper', 'Knowledge search', 'Themes', 'Monitoring', 'Research', 'Gaps', 'Analyst cards']) {
        expect(await within(main).findByText(title, {}, FIRST_PAINT)).toBeInTheDocument();
      }
      const themesTile = within(main).getByLabelText('Themes');
      expect(await within(themesTile).findByText('1 of 2 projects with results', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(within(within(main).getByLabelText('Gaps')).getByText('0 of 2 projects with results')).toBeInTheDocument();

      await user.click(themesTile);
      await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/modules/themes'), FIRST_PAINT);
      expect(await screen.findByText('9 themes · built 2 Nov 2026', { exact: false }, FIRST_PAINT)).toBeInTheDocument();
      expect(screen.getByText('Theme map not built yet')).toBeInTheDocument();
      expect(screen.getByText(/Projects are shared with every module/)).toBeInTheDocument();
      const card = screen.getByText('Life cover').closest('.ant-card') as HTMLElement;
      expect(within(card).getByLabelText('Open Life cover')).toHaveAttribute('href', '/projects/p1?tab=themes');

      await user.click(within(card).getByText('Life cover'));
      await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/projects/p1?tab=themes'), FIRST_PAINT);
      const crumbs = await waitFor(() => {
        const found = document.querySelector<HTMLElement>('.ant-breadcrumb');
        if (!found) throw new Error('no breadcrumb yet');
        return found;
      }, FIRST_PAINT);
      expect(await within(crumbs).findByText('Themes', {}, FIRST_PAINT)).toHaveAttribute('href', '/modules/themes');
      expect(within(crumbs).getByText('Home')).toHaveAttribute('href', '/');
    },
    TIMEOUT,
  );

  it(
    'creates a project from a module and opens it on that module; unknown modules are said so',
    async () => {
      const posted = fakeServer({
        ...routes,
        'POST /api/projects': () => [200, { project, token: 'tok-1', expires_at: '2099-01-01T00:00:00Z' }],
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/modules/gaps');
      await screen.findByText('Life cover', {}, FIRST_PAINT);
      const button = screen.getAllByText('New project')[0]?.closest('button');
      if (!button) throw new Error('no New project button');
      await user.click(button);
      const dialog = await waitFor(() => {
        const found = document.querySelector<HTMLElement>('.ant-modal-content');
        if (!found) throw new Error('no dialog yet');
        return found;
      }, FIRST_PAINT);
      for (const [label, text] of [
        ['Project name', 'Life cover'],
        ['Your name', 'Gaurav'],
        ['Client name', 'Example'],
        ['Project password', 'correct horse 42'],
        ['Repeat the password', 'correct horse 42'],
      ] as const) {
        await user.click(within(dialog).getByLabelText(label));
        await user.paste(text);
      }
      const create = within(dialog).getByText('Create project').closest('button') as HTMLElement;
      await user.click(create);
      // no competitor named: the form asks once, and nothing is sent yet
      expect(await within(dialog).findByText('No competitors yet', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(posted).toHaveLength(0);
      await user.click(within(dialog).getByText('Create without competitors').closest('button') as HTMLElement);
      await waitFor(() => expect(posted).toHaveLength(1), FIRST_PAINT);
      await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/projects/p1?tab=gaps'), FIRST_PAINT);
    },
    TIMEOUT,
  );

  it('says when a module does not exist', async () => {
    fakeServer(routes);
    renderApp('/modules/nope');
    expect(await screen.findByText('There is no such module.', {}, FIRST_PAINT)).toBeInTheDocument();
  });
});
