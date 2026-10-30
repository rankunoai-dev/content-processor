import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectTokens, type Project } from '../api/projects';
import { renderApp } from './utils';

const project: Project = {
  project_id: 'print-security-a1b2c3',
  name: 'Print security',
  owner_name: 'Gaurav',
  client_name: "Flynn's",
  created_at: '2026-10-30T09:00:00Z',
  updated_at: '2026-10-30T09:00:00Z',
  url_count: 2,
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

const readRoutes = {
  'GET /api/projects': () => [200, [project]] as [number, unknown],
  [`GET /api/projects/${project.project_id}`]: () => [200, project] as [number, unknown],
  [`GET /api/projects/${project.project_id}/urls`]: () =>
    [200, { total: 2, items: [{ url: 'https://www.flynns.com/blog/a', added_at: '2026-10-30T09:00:00Z' }] }] as [number, unknown],
  [`GET /api/projects/${project.project_id}/runs`]: () => [200, []] as [number, unknown],
};

beforeEach(() => useProjectTokens.setState({ tokens: {} }));
afterEach(() => vi.unstubAllGlobals());

// The two interaction tests open forms and dialogs; under a loaded machine they need
// more than the suite's 20 s default.
const INTERACTION_TIMEOUT_MS = 60_000;

describe('projects (Content Scraper)', () => {
  it('lists projects with their client and owner', async () => {
    fakeServer(readRoutes);
    renderApp('/projects');
    expect(await screen.findByText('Print security')).toBeInTheDocument();
    expect(screen.getByText(/Client: Flynn's/)).toBeInTheDocument();
    expect(screen.getByText(/Owner: Gaurav/)).toBeInTheDocument();
    expect(screen.getByText('Not scraped yet')).toBeInTheDocument();
  });

  it('creates a project, checks the password twice, and opens it unlocked', async () => {
    const sent = fakeServer({
      ...readRoutes,
      'POST /api/projects': () => [200, { project, token: 'tok-1', expires_at: '2099-01-01T00:00:00Z' }],
    });
    const user = userEvent.setup();
    renderApp('/projects');
    const [newProject] = await screen.findAllByRole('button', { name: /New project/ });
    if (!newProject) throw new Error('no New project button');
    await user.click(newProject);
    const dialog = await screen.findByRole('dialog');
    await fill(user, within(dialog).getByLabelText('Project name'), 'Print security');
    await fill(user, within(dialog).getByLabelText('Your name'), 'Gaurav');
    await fill(user, within(dialog).getByLabelText('Client name'), "Flynn's");
    await fill(user, within(dialog).getByLabelText('Project password'), 'correct horse 42');
    await fill(user, within(dialog).getByLabelText('Repeat the password'), 'something else');
    await user.click(within(dialog).getByRole('button', { name: 'Create project' }));
    expect(await within(dialog).findByText('The two passwords are not the same.')).toBeInTheDocument();
    expect(sent).toHaveLength(0);

    await user.clear(within(dialog).getByLabelText('Repeat the password'));
    await fill(user, within(dialog).getByLabelText('Repeat the password'), 'correct horse 42');
    await user.click(within(dialog).getByRole('button', { name: 'Create project' }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.body).toEqual({
      name: 'Print security',
      owner_name: 'Gaurav',
      client_name: "Flynn's",
      password: 'correct horse 42',
    });
    expect(sent[0]?.headers['X-Requested-With']).toBe('content-processor');
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(`/projects/${project.project_id}`));
    expect(await screen.findByText('You can make changes')).toBeInTheDocument();
  }, INTERACTION_TIMEOUT_MS);

  it('is view-only until unlocked; changes carry the unlock token', async () => {
    const sent = fakeServer({
      ...readRoutes,
      [`POST /api/projects/${project.project_id}/unlock`]: (body) =>
        (body as { password: string }).password === 'correct horse 42'
          ? [200, { project, token: 'tok-2', expires_at: '2099-01-01T00:00:00Z' }]
          : [401, { detail: 'That password is not right.' }],
      [`POST /api/projects/${project.project_id}/urls`]: () => [
        200,
        { added: 1, already_present: 0, duplicates_in_input: 0, rejected: 1, over_limit: 0, url_count: 3,
          rejected_examples: [{ value: 'ftp://x', reason: 'only http and https addresses can be scraped' }] },
      ],
    });
    const user = userEvent.setup();
    renderApp(`/projects/${project.project_id}`);
    expect(await screen.findByText('View only')).toBeInTheDocument();
    const paste = screen.getByLabelText('Paste addresses, one per line');
    expect(paste).toBeDisabled();

    await user.click(screen.getByRole('button', { name: /Unlock to make changes/ }));
    const dialog = await screen.findByRole('dialog');
    await fill(user, within(dialog).getByLabelText('Password'), 'wrong one');
    await user.click(within(dialog).getByRole('button', { name: 'Unlock' }));
    expect(await within(dialog).findByText('That password is not right.')).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText('Password'));
    await fill(user, within(dialog).getByLabelText('Password'), 'correct horse 42');
    await user.click(within(dialog).getByRole('button', { name: 'Unlock' }));
    expect(await screen.findByText('You can make changes')).toBeInTheDocument();

    await fill(user, screen.getByLabelText('Paste addresses, one per line'), 'https://www.flynns.com/blog/b\nftp://x');
    await user.click(screen.getByRole('button', { name: 'Add to the list' }));
    expect(await screen.findByText(/1 added · 0 already in the list · 0 repeated · 1 not usable/)).toBeInTheDocument();
    expect(screen.getByText('only http and https addresses can be scraped')).toBeInTheDocument();
    const add = sent.find((s) => s.path.endsWith('/urls'));
    expect(add?.headers['X-Project-Token']).toBe('tok-2');
    expect(add?.body).toEqual({ text: 'https://www.flynns.com/blog/b\nftp://x' });
  }, INTERACTION_TIMEOUT_MS);
});
