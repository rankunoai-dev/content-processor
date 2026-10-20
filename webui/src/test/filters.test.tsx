import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { pagesQueryString } from '../api/client';
import { DEFAULT_VIEW, parseViewParams, serializeViewParams, toPageFilters } from '../state/viewParams';
import { mockApi, overview, pagesResponse, renderApp } from './utils';

afterEach(() => vi.unstubAllGlobals());

describe('view state in the URL', () => {
  it('round-trips every field', () => {
    const view = {
      ...DEFAULT_VIEW,
      status: ['PARTIAL', 'BLOCKED'],
      host: ['example.com'],
      format: ['pdf'],
      issue: ['UNVISITED_STATE'],
      hasIssues: true,
      q: 'price list',
      sort: 'words' as const,
      order: 'desc' as const,
      page: 3,
      size: 100,
      tab: 'issues' as const,
      areas: ['main' as const, 'footer' as const],
      hidden: true,
      unconfirmed: true,
      find: 'fee',
      bucket: 'context_matched',
    };
    expect(parseViewParams(serializeViewParams(view))).toEqual(view);
  });

  it('keeps defaults out of the URL and ignores bad values', () => {
    expect(serializeViewParams(DEFAULT_VIEW).toString()).toBe('');
    const parsed = parseViewParams(new URLSearchParams('sort=evil&page=-4&size=7&tab=nope&areas=main,bogus'));
    expect(parsed.sort).toBe('status');
    expect(parsed.page).toBe(1);
    expect(parsed.size).toBe(50);
    expect(parsed.tab).toBe('content');
    expect(parsed.areas).toEqual(['main']);
  });

  it('builds the API query from the view', () => {
    const qs = pagesQueryString(toPageFilters({ ...DEFAULT_VIEW, status: ['PARTIAL'], hasIssues: true }));
    expect(qs).toContain('status=PARTIAL');
    expect(qs).toContain('has_issues=true');
  });
});

describe('pages screen filters', () => {
  it('reads filters from the URL and writes changes back to it', async () => {
    const calls = mockApi({
      '/api/runs': () => ({ exports_dir: 'x', runs: [] }),
      '/api/runs/demo/overview': () => overview,
      '/api/runs/demo/pages': () => pagesResponse,
    });
    renderApp('/runs/demo/pages?status=PARTIAL&q=pricing');
    expect(await screen.findByText('Pricing plans')).toBeInTheDocument();
    const first = calls.find((u) => u.pathname === '/api/runs/demo/pages');
    expect(first?.searchParams.getAll('status')).toEqual(['PARTIAL']);
    expect(first?.searchParams.get('q')).toBe('pricing');

    await userEvent.click(screen.getByRole('switch', { name: 'Only pages with issues' }));
    await waitFor(() => expect(screen.getByTestId('location').textContent).toContain('has_issues=true'));
    await waitFor(() =>
      expect(calls.some((u) => u.pathname === '/api/runs/demo/pages' && u.searchParams.get('has_issues') === 'true')).toBe(true),
    );

    await userEvent.click(screen.getByRole('button', { name: /Clear filters/ }));
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/runs/demo/pages'));
  });

  it('explains an empty result', async () => {
    mockApi({
      '/api/runs': () => ({ exports_dir: 'x', runs: [] }),
      '/api/runs/demo/overview': () => overview,
      '/api/runs/demo/pages': () => ({ ...pagesResponse, total: 0, items: [] }),
    });
    renderApp('/runs/demo/pages?q=zzz');
    expect(await screen.findByText(/No pages match these filters/)).toBeInTheDocument();
  });
});
