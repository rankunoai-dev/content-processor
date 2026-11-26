import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { App as AntApp } from 'antd';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AnalyticsReport, AnalyticsStatus } from '../api/analytics';
import { AnalyticsCard } from '../routes/AnalyticsCard';

const report: AnalyticsReport = {
  project_id: 'p1',
  website: 'example.com',
  built_at: '2026-11-26T10:00:00Z',
  snapshot_id: 'ga4-20261126-100000-000',
  previous_snapshot_id: 'ga4-20261020-100000-000',
  period: '2026-10-01 to 2026-10-28',
  totals: { sessions: 1260, organic_sessions: 830, engagement_rate: 0.5, key_events: 41, revenue: 0, ai_sessions: 12 },
  channels: { 'Organic Search': 830, Direct: 430 },
  themes: [
    { key: 'th-ann', label: 'Annuities', pages: 1, sessions: 400, organic_sessions: 300, engaged_sessions: 320, key_events: 40, revenue: 0, search_volume: 8100 },
  ],
  funnel: [],
  leaky: [{ url: 'https://example.com/life/term', organic_sessions: 500, gsc_clicks: null, engagement_rate: 0.2, key_events: 0, reason: 'only 20% of sessions engage' }],
  decays: [{ url: 'https://example.com/life/term', metric: 'organic_sessions', before: 900, after: 500, change: -0.444 }],
  cta_gaps: [{ url: 'https://example.com/contact', sessions: 200, ctas: ['Contact an advisor'], forms: 1 }],
  ai_referrals: [{ url: 'https://example.com/blog/ai', sessions: 12, sources: { 'chatgpt.com': 12 } }],
  post_edit: [],
  notes: [],
};

const off: AnalyticsStatus = { enabled: false, credentials_set: false, pull_refused_because: 'GA4 pulls are switched off', max_requests: 20 };

interface Sent {
  path: string;
  body: unknown;
  method: string;
}

function serve(routes: Record<string, () => [number, unknown]>): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1');
      const method = init?.method ?? 'GET';
      if (method !== 'GET') sent.push({ path: url.pathname, method, body: init?.body ? JSON.parse(String(init.body)) : null });
      const handler = routes[`${method} ${url.pathname}`];
      const [code, payload] = handler ? handler() : [404, { detail: 'There is no such thing.' }];
      return new Response(JSON.stringify(payload), { status: code, headers: { 'Content-Type': 'application/json' } });
    }),
  );
  return sent;
}

function show(token: string | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AntApp>
        <AnalyticsCard projectId="p1" token={token} />
      </AntApp>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('analytics card', () => {
  it('shows totals and themes, says why pulls are off, and saves a property once unlocked', async () => {
    const sent = serve({
      'GET /api/analytics/status': () => [200, off],
      'GET /api/projects/p1/analytics': () => [200, report],
      'GET /api/projects/p1/analytics/settings': () => [200, { property_id: null, website: 'example.com' }],
      'POST /api/projects/p1/analytics/settings': () => [200, { property_id: '123456789', website: 'example.com' }],
    });
    const { container } = show('tok-1');
    expect(await screen.findByText(/1,260 sessions, 830 from search/, {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.getByText('Annuities')).toBeInTheDocument();
    expect(screen.getByText('GA4 pulls are off: GA4 pulls are switched off')).toBeInTheDocument();
    expect(screen.getByText('Pull from GA4').closest('button')).toBeDisabled();
    expect(screen.getByText('Traffic that does not convert (1)')).toBeInTheDocument();
    const download = screen.getByText('Download the analytics workbook').closest('a') as HTMLAnchorElement;
    expect(download.getAttribute('href')).toBe('/api/projects/p1/analytics/workbook');
    const input = container.querySelector('input[aria-label="GA4 property id (numbers)"]') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '123456789' } });
    fireEvent.click(screen.getByText('Save property'));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({ path: '/api/projects/p1/analytics/settings', method: 'POST', body: { property_id: '123456789', website: null } });
  }, 60_000);

  it('asks for the unlock and shows the empty state without data', async () => {
    serve({
      'GET /api/analytics/status': () => [200, off],
      'GET /api/projects/p1/analytics': () => [404, { detail: 'No GA4 data has been added yet.' }],
      'GET /api/projects/p1/analytics/settings': () => [200, { property_id: null, website: null }],
    });
    show(null);
    expect(await screen.findByText('No GA4 data has been added to this project yet.', {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.getByText('Choose the GA4 CSV').closest('button')).toBeDisabled();
  }, 60_000);
});
