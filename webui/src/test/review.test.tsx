import { screen, waitFor, within } from '@testing-library/react';
import userEvent, { PointerEventsCheckLevel } from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Me } from '../api/auth';
import type { Project, RunState } from '../api/projects';
import type { VerdictList } from '../api/verdicts';
import { renderApp } from './utils';

const FIRST_PAINT = { timeout: 15_000 };

const project = {
  project_id: 'p1',
  name: 'Life cover',
  owner_name: 'Gaurav',
  client_name: 'Example',
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-01T09:00:00Z',
  url_count: 2,
  confidential: false,
  sites: [
    {
      site_id: 's1',
      name: 'Example',
      role: 'client',
      main: true,
      domains: ['example.com'],
      note: '',
      tags: [],
      url_count: 2,
      last_run: null,
      namespace: 'n1',
    },
  ],
  last_run: null,
} as unknown as Project;

const run = {
  run_id: 'r1',
  project_id: 'p1',
  site_id: 's1',
  status: 'finished',
  created_at: '2026-11-10T09:00:00Z',
} as unknown as RunState;

function list(decided: Record<string, { decision: 'approved' | 'rejected'; next_step: string; note: string }>): VerdictList {
  const page = (url: string, title: string, badge: string, gaps: string[]) => {
    const d = decided[url];
    const decision = d
      ? { ...d, next_step: d.next_step as '', reviewer: 'Rita Reviewer', at: '2026-11-17T10:00:00Z' }
      : null;
    return { url, title, badge, gaps, is_page: true, note: '', decision, history: decision ? [decision] : [] };
  };
  return {
    run_id: 'r1',
    site_id: 's1',
    pages: [
      page('https://example.com/term', 'Term life', 'PARTIAL', ['VISIBLE_TEXT_NOT_EXTRACTED']),
      page('https://example.com/about', 'About us', 'VERIFIED_WITHIN_SCOPE', []),
    ],
    counts: { undecided: 2 - Object.keys(decided).length },
  };
}

afterEach(() => vi.unstubAllGlobals());

describe('review tab', () => {
  it('approves and rejects verdicts with a reason and a next step', async () => {
    const decided: Record<string, { decision: 'approved' | 'rejected'; next_step: string; note: string }> = {};
    const sent: unknown[] = [];
    const me: Me = {
      accounts: true,
      min_password: 10,
      account: {
        username: 'rita',
        display_name: 'Rita Reviewer',
        role: 'reviewer',
        must_change_password: false,
        disabled: false,
        created_at: '2026-11-16T09:00:00Z',
        created_by: '',
        last_login_at: null,
      },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), 'http://127.0.0.1').pathname;
        const method = init?.method ?? 'GET';
        let payload: unknown = { detail: 'There is no such thing.' };
        let status = 404;
        if (path === '/api/auth/me') [status, payload] = [200, me];
        if (path === '/api/projects/p1') [status, payload] = [200, project];
        if (path === '/api/projects/p1/runs') [status, payload] = [200, [run]];
        if (path === '/api/projects/p1/sites/s1/runs/r1/verdicts' && method === 'GET') [status, payload] = [200, list(decided)];
        if (path === '/api/projects/p1/sites/s1/runs/r1/verdicts' && method === 'POST') {
          const body = JSON.parse(String(init?.body)) as { url: string; decision: 'approved' | 'rejected'; next_step?: string; note?: string };
          sent.push(body);
          decided[body.url] = { decision: body.decision, next_step: body.next_step ?? '', note: body.note ?? '' };
          [status, payload] = [200, { ...decided[body.url], reviewer: 'Rita Reviewer', at: '2026-11-17T10:00:00Z' }];
        }
        return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
      }),
    );
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderApp('/projects/p1?tab=review');
    expect(await screen.findByText('Term life', {}, FIRST_PAINT)).toBeInTheDocument();
    expect(screen.queryByText('About us')).not.toBeInTheDocument(); // fully captured: not "needs attention"
    await user.click(screen.getByText('All pages'));
    expect(await screen.findByText('About us')).toBeInTheDocument();
    expect(screen.queryByLabelText('Your name')).not.toBeInTheDocument(); // the account names the reviewer

    await user.click(screen.getByRole('button', { name: 'Approve: https://example.com/about' }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({ url: 'https://example.com/about', decision: 'approved' });
    const about = (await screen.findByText('About us')).closest('tr') as HTMLElement;
    expect(await within(about).findByText('Approved')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Reject: https://example.com/term' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Reject' }));
    expect(await within(dialog).findByText('Say why the verdict is wrong.')).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText('Why'), 'the rate table is missing');
    await user.click(within(dialog).getByText('Scrape again'));
    await user.click(within(dialog).getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1]).toEqual({
      url: 'https://example.com/term',
      decision: 'rejected',
      note: 'the rate table is missing',
      next_step: 'scrape_again',
    });
    const term = (await screen.findByText('Term life')).closest('tr') as HTMLElement;
    expect(await within(term).findByText('Scrape again')).toBeInTheDocument();
    expect(within(term).getByText('the rate table is missing')).toBeInTheDocument();
  }, 60_000);
});
