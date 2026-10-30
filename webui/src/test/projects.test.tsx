import { screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectTokens, type Project, type Site } from '../api/projects';
import { renderApp } from './utils';

const site = (site_id: string, name: string, role: Site['role'], domain: string, url_count: number): Site => ({
  site_id,
  name,
  role,
  domains: [domain],
  url_count,
  last_run: null,
  namespace: `rankuno-dev.print-security-a1b2c3.${site_id}`,
});

const project: Project = {
  project_id: 'print-security-a1b2c3',
  name: 'Print security',
  owner_name: 'Gaurav',
  client_name: "Flynn's",
  created_at: '2026-10-30T09:00:00Z',
  updated_at: '2026-10-30T09:00:00Z',
  url_count: 3,
  sites: [site('flynn-s', "Flynn's", 'client', 'flynns.com', 2), site('xerox', 'Xerox', 'competitor', 'xerox.com', 1)],
  last_run: null,
};

interface Sent {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

/** A fake server: GET and POST routes by path; records every POST with its headers. */
function fakeServer(routes: Record<string, (body: unknown) => [number, unknown]>) {
  const sent: Sent[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1');
      const method = init?.method ?? 'GET';
      const body: unknown = init?.body ? JSON.parse(String(init.body)) : undefined;
      if (method === 'POST') {
        sent.push({ method, path: url.pathname, body, headers: (init?.headers ?? {}) as Record<string, string> });
      }
      const handler = routes[`${method} ${url.pathname}`];
      const [status, payload] = handler ? handler(body) : [404, { detail: 'There is no such thing.' }];
      return new Response(JSON.stringify(payload), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
  return sent;
}

/** Put text into a field the way a paste does: one input event, not one per key. */
async function fill(user: ReturnType<typeof userEvent.setup>, field: HTMLElement, text: string) {
  await user.click(field);
  await user.paste(text);
}

/**
 * The button showing this text. Text queries are used instead of role queries: on a
 * screen this large, computing accessible names for every element takes seconds in jsdom.
 */
function buttonIn(scope: Pick<typeof screen, 'getByText'>, text: string | RegExp): HTMLElement {
  const button = scope.getByText(text).closest('button');
  if (!button) throw new Error(`no button showing ${String(text)}`);
  return button;
}

const readRoutes = {
  'GET /api/projects': () => [200, [project]] as [number, unknown],
  [`GET /api/projects/${project.project_id}`]: () => [200, project] as [number, unknown],
  [`GET /api/projects/${project.project_id}/sites/flynn-s/urls`]: () =>
    [200, { total: 2, items: [{ url: 'https://www.flynns.com/blog/a', added_at: '2026-10-30T09:00:00Z' }] }] as [number, unknown],
  [`GET /api/projects/${project.project_id}/runs`]: () => [200, []] as [number, unknown],
};

beforeEach(() => useProjectTokens.setState({ tokens: {} }));
afterEach(() => vi.unstubAllGlobals());

// The two interaction tests open forms and dialogs; under a loaded machine they need
// more than the suite's 20 s default.
const INTERACTION_TIMEOUT_MS = 120_000;
/** The first render of a lazily loaded screen can take several seconds in jsdom. */
const FIRST_PAINT = { timeout: 15_000 };

describe('projects (Content Scraper)', () => {
  it('lists projects with their client, owner and websites', async () => {
    fakeServer(readRoutes);
    renderApp('/projects');
    expect(await screen.findByText('Print security', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(screen.getByText(/Client: Flynn's/)).toBeInTheDocument();
    expect(screen.getByText(/Owner: Gaurav/)).toBeInTheDocument();
    expect(screen.getByText('Xerox')).toBeInTheDocument();
    expect(screen.getByText(/2 websites · 3 addresses/)).toBeInTheDocument();
    expect(screen.getByText('Not scraped yet')).toBeInTheDocument();
  });

  it('creates a project with a competitor, checks the password twice, opens it unlocked', async () => {
    const sent = fakeServer({
      ...readRoutes,
      'POST /api/projects': () => [200, { project, token: 'tok-1', expires_at: '2099-01-01T00:00:00Z' }],
    });
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderApp('/projects');
    await screen.findByText('Print security', {}, FIRST_PAINT);
    const [newProject] = screen.getAllByText('New project');
    const newButton = newProject?.closest('button');
    if (!newButton) throw new Error('no New project button');
    await user.click(newButton);
    const dialog = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('.ant-modal-content');
      if (!found) throw new Error('no dialog yet');
      return found;
    }, FIRST_PAINT);
    await fill(user, within(dialog).getByLabelText('Project name'), 'Print security');
    await fill(user, within(dialog).getByLabelText('Your name'), 'Gaurav');
    await fill(user, within(dialog).getByLabelText('Client name'), "Flynn's");
    await fill(user, within(dialog).getByLabelText("Client's website"), 'flynns.com');
    await user.click(buttonIn(within(dialog), 'Add a competitor'));
    await fill(user, within(dialog).getByLabelText('Competitor name 1'), 'Xerox');
    await fill(user, within(dialog).getByLabelText('Website 1'), 'xerox.com');
    await fill(user, within(dialog).getByLabelText('Project password'), 'correct horse 42');
    await fill(user, within(dialog).getByLabelText('Repeat the password'), 'something else');
    await user.click(buttonIn(within(dialog), 'Create project'));
    expect(await within(dialog).findByText('The two passwords are not the same.')).toBeInTheDocument();
    expect(sent).toHaveLength(0);

    await user.clear(within(dialog).getByLabelText('Repeat the password'));
    await fill(user, within(dialog).getByLabelText('Repeat the password'), 'correct horse 42');
    await user.click(buttonIn(within(dialog), 'Create project'));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({
      name: 'Print security',
      owner_name: 'Gaurav',
      client_name: "Flynn's",
      password: 'correct horse 42',
      client_website: 'flynns.com',
      competitors: [{ name: 'Xerox', website: 'xerox.com' }],
    });
    expect(sent[0]?.headers['X-Requested-With']).toBe('content-processor');
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(`/projects/${project.project_id}`));
    expect(await screen.findByText('You can make changes', {}, FIRST_PAINT)).toBeInTheDocument();
  }, INTERACTION_TIMEOUT_MS);

  it('is view-only until unlocked; changes carry the unlock token', async () => {
    const sent = fakeServer({
      ...readRoutes,
      [`POST /api/projects/${project.project_id}/unlock`]: (body) =>
        (body as { password: string }).password === 'correct horse 42'
          ? [200, { project, token: 'tok-2', expires_at: '2099-01-01T00:00:00Z' }]
          : [401, { detail: 'That password is not right.' }],
      [`POST /api/projects/${project.project_id}/sites/flynn-s/urls`]: () => [
        200,
        { site_id: 'flynn-s', site_name: "Flynn's", added: 1, already_present: 0, duplicates_in_input: 0, rejected: 1, over_limit: 0, url_count: 3,
          rejected_examples: [{ value: 'ftp://x', reason: 'only http and https addresses can be scraped' }] },
      ],
    });
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderApp(`/projects/${project.project_id}`);
    expect(await screen.findByText('View only', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(screen.getByText('Xerox').closest('[role="tab"]')).not.toBeNull();
    const paste = screen.getByLabelText('Paste addresses, one per line');
    expect(paste).toBeDisabled();

    await user.click(buttonIn(screen, 'Unlock to make changes'));
    const dialog = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('.ant-modal-content');
      if (!found) throw new Error('no dialog yet');
      return found;
    }, FIRST_PAINT);
    await fill(user, within(dialog).getByLabelText('Password'), 'wrong one');
    await user.click(buttonIn(within(dialog), 'Unlock'));
    expect(await within(dialog).findByText('That password is not right.')).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText('Password'));
    await fill(user, within(dialog).getByLabelText('Password'), 'correct horse 42');
    await user.click(buttonIn(within(dialog), 'Unlock'));
    expect(await screen.findByText('You can make changes')).toBeInTheDocument();

    await fill(user, screen.getByLabelText('Paste addresses, one per line'), 'https://www.flynns.com/blog/b\nftp://x');
    await user.click(buttonIn(screen, 'Add to the list'));
    expect(await screen.findByText(/1 added · 0 already in the list · 0 repeated · 1 not usable/)).toBeInTheDocument();
    expect(screen.getByText('only http and https addresses can be scraped')).toBeInTheDocument();
    const add = sent.find((s) => s.path.endsWith('/sites/flynn-s/urls'));
    expect(add?.headers['X-Project-Token']).toBe('tok-2');
    expect(add?.body).toEqual({ text: 'https://www.flynns.com/blog/b\nftp://x' });
  }, INTERACTION_TIMEOUT_MS);
  it('searches the client and its competitors from every project by default', async () => {
    const asked: URL[] = [];
    fakeServer({
      ...readRoutes,
      'GET /api/knowledge/search': () => [
        200,
        {
          query: 'secure print',
          scope: ["Flynn's", 'HP', 'Xerox'],
          hits: [
            { rank: 1, score: 0.03, website: 'hp.com', project_id: 'hp-refresh-1', project_name: 'HP refresh',
              site_id: 'hp', site_name: 'HP', role: 'client', url: 'https://www.hp.com/p', headings: ['Security'],
              text: 'HP secure print release for offices.', chunk_id: 'c1', evidence_ids: [] },
          ],
          searched: ['a', 'b'],
          skipped: [{ space: 'canon.com', site: 'Canon', reason: 'not scraped yet in any project' }],
        },
      ],
    });
    const realFetch = globalThis.fetch;
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1');
      if (url.pathname === '/api/knowledge/search') asked.push(url);
      return realFetch(input, init);
    });
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderApp(`/projects/${project.project_id}?tab=search`);
    const box = await screen.findByPlaceholderText('For example: secure print release', {}, FIRST_PAINT);
    await fill(user, box, 'secure print');
    await user.keyboard('{Enter}');
    expect(await screen.findByText('HP secure print release for offices.', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(asked[0]?.searchParams.get('website')).toBe('flynns.com');
    expect(asked[0]?.searchParams.getAll('space')).toEqual([]);
    expect(screen.getByText("Websites considered: Flynn's, HP, Xerox")).toBeInTheDocument();
    expect(screen.getByText('from project HP refresh')).toBeInTheDocument();
    expect(screen.getByText('Canon was not searched: not scraped yet in any project')).toBeInTheDocument();
  }, INTERACTION_TIMEOUT_MS);
});
