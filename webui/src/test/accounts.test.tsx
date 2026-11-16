import { screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Account, Me } from '../api/auth';
import type { Project } from '../api/projects';
import { renderApp } from './utils';

const FIRST_PAINT = { timeout: 15_000 };
const TIMEOUT = 60_000;

function account(role: Account['role'], extra: Partial<Account> = {}): Account {
  return {
    username: role === 'admin' ? 'gaurav' : `${role}1`,
    display_name: role === 'admin' ? 'Gaurav' : `A ${role}`,
    role,
    must_change_password: false,
    disabled: false,
    created_at: '2026-11-16T09:00:00Z',
    created_by: '',
    last_login_at: null,
    ...extra,
  };
}

const project: Project = {
  project_id: 'p1',
  name: 'Life cover',
  owner_name: 'Gaurav',
  client_name: 'Example',
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-01T09:00:00Z',
  url_count: 0,
  sites: [
    {
      site_id: 's1',
      name: 'Example',
      role: 'client',
      main: true,
      domains: ['example.com'],
      note: '',
      tags: [],
      url_count: 0,
      last_run: null,
      namespace: 'n1',
    },
  ],
  last_run: null,
  confidential: false,
};

interface Sent {
  path: string;
  body: unknown;
}

function server(state: { me: Me }, extra: Record<string, (body: unknown) => [number, unknown]> = {}) {
  const sent: Sent[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), 'http://127.0.0.1').pathname;
      const method = init?.method ?? 'GET';
      const body: unknown = init?.body ? JSON.parse(String(init.body)) : undefined;
      if (method === 'POST') sent.push({ path, body });
      const routes: Record<string, (b: unknown) => [number, unknown]> = {
        'GET /api/auth/me': () => [200, state.me],
        'GET /api/projects': () => [200, [project]],
        'GET /api/projects/p1': () => [200, project],
        'GET /api/projects/p1/runs': () => [200, []],
        ...extra,
      };
      const handler = routes[`${method} ${path}`];
      const [status, payload] = handler ? handler(body) : [404, { detail: 'There is no such thing.' }];
      return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
    }),
  );
  return sent;
}

afterEach(() => vi.unstubAllGlobals());

describe('accounts', () => {
  it(
    'asks to sign in, says when the password is wrong, then opens the app',
    async () => {
      const state: { me: Me } = { me: { accounts: true, account: null, min_password: 10 } };
      const sent = server(state, {
        'POST /api/auth/login': (b) => {
          const { password } = b as { password: string };
          if (password !== 'right-password-1') return [401, { detail: 'That user name and password do not match.' }];
          state.me = { accounts: true, account: account('editor'), min_password: 10 };
          return [200, state.me];
        },
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects');
      expect(await screen.findByRole('heading', { name: 'Sign in' }, FIRST_PAINT)).toBeInTheDocument();
      expect(screen.queryByText('Life cover')).not.toBeInTheDocument();
      await user.type(screen.getByLabelText('User name'), 'Editor1');
      await user.type(screen.getByLabelText('Password'), 'wrong');
      await user.click(screen.getByRole('button', { name: 'Sign in' }));
      expect(await screen.findByText('That user name and password do not match.')).toBeInTheDocument();
      await user.clear(screen.getByLabelText('Password'));
      await user.type(screen.getByLabelText('Password'), 'right-password-1');
      await user.click(screen.getByRole('button', { name: 'Sign in' }));
      expect(await screen.findByText('Life cover', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(sent.at(-1)).toEqual({ path: '/api/auth/login', body: { username: 'Editor1', password: 'right-password-1' } });
      expect(screen.getByRole('button', { name: 'Your account' })).toHaveTextContent('A editor');
    },
    TIMEOUT,
  );

  it(
    'makes a person with a temporary password choose their own first',
    async () => {
      const state: { me: Me } = {
        me: { accounts: true, account: account('reviewer', { must_change_password: true }), min_password: 10 },
      };
      const sent = server(state, {
        'POST /api/auth/password': () => {
          state.me = { accounts: true, account: account('reviewer'), min_password: 10 };
          return [200, state.me];
        },
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/projects');
      expect(await screen.findByText('Welcome, A reviewer', {}, FIRST_PAINT)).toBeInTheDocument();
      await user.type(screen.getByLabelText('Current password'), 'temporary-pw-1');
      await user.type(screen.getByLabelText('New password'), 'short');
      await user.type(screen.getByLabelText('New password again'), 'short');
      await user.click(screen.getByRole('button', { name: 'Save the new password' }));
      expect(await screen.findAllByText('At least 10 characters.')).not.toHaveLength(0);
      expect(sent).toHaveLength(0);
      await user.clear(screen.getByLabelText('New password'));
      await user.clear(screen.getByLabelText('New password again'));
      await user.type(screen.getByLabelText('New password'), 'my-own-password');
      await user.type(screen.getByLabelText('New password again'), 'my-own-password');
      await user.click(screen.getByRole('button', { name: 'Save the new password' }));
      expect(await screen.findByText('Life cover', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(sent[0]).toEqual({
        path: '/api/auth/password',
        body: { current: 'temporary-pw-1', new: 'my-own-password' },
      });
    },
    TIMEOUT,
  );

  it(
    'shows a viewer the project read-only and an editor without any unlock step',
    async () => {
      const state: { me: Me } = { me: { accounts: true, account: account('viewer'), min_password: 10 } };
      server(state);
      const first = renderApp('/projects/p1');
      expect(await screen.findByText('Your role (Viewer) cannot change projects.', {}, FIRST_PAINT)).toBeInTheDocument();
      expect(screen.queryByText('Unlock to make changes')).not.toBeInTheDocument();
      first.unmount();
      state.me = { accounts: true, account: account('editor'), min_password: 10 };
      renderApp('/projects/p1');
      expect(await screen.findAllByText('Life cover', {}, FIRST_PAINT)).not.toHaveLength(0);
      expect(screen.queryByText(/cannot change projects/)).not.toBeInTheDocument();
      expect(screen.queryByText('Unlock to make changes')).not.toBeInTheDocument();
    },
    TIMEOUT,
  );

  it(
    'lets an admin add an account with a temporary password',
    async () => {
      const state: { me: Me } = { me: { accounts: true, account: account('admin'), min_password: 10 } };
      let list: Account[] = [account('admin')];
      const sent = server(state, {
        'GET /api/accounts': () => [200, list],
        'POST /api/accounts': (b) => {
          const body = b as { username: string; display_name: string; role: Account['role'] };
          const created = account(body.role, {
            username: body.username,
            display_name: body.display_name,
            must_change_password: true,
          });
          list = [...list, created];
          return [200, created];
        },
      });
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      renderApp('/accounts');
      expect(await screen.findByText('Gaurav', {}, FIRST_PAINT)).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: /Add an account/ }));
      const dialog = await screen.findByRole('dialog');
      await user.type(within(dialog).getByLabelText('Name'), 'Anna Smith');
      await user.type(within(dialog).getByLabelText('User name'), 'Anna');
      await user.type(within(dialog).getByLabelText('Temporary password'), 'temporary-pw-1');
      await user.click(within(dialog).getByRole('button', { name: 'Add' }));
      await waitFor(() => expect(sent).toHaveLength(1));
      expect(sent[0]?.body).toEqual({
        username: 'anna',
        display_name: 'Anna Smith',
        role: 'viewer',
        password: 'temporary-pw-1',
      });
      expect(await screen.findByText('Waiting for first sign-in')).toBeInTheDocument();
    },
    TIMEOUT,
  );

  it('keeps the Accounts page for admins', async () => {
    server({ me: { accounts: true, account: account('editor'), min_password: 10 } });
    renderApp('/accounts');
    expect(await screen.findByText('Only admins can manage accounts.', {}, FIRST_PAINT)).toBeInTheDocument();
  }, TIMEOUT);
});
