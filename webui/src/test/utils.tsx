import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { vi } from 'vitest';
import { AppRoutes, ThemeProvider } from '../App';
import type { PageDetail, PageRow, PagesResponse, Overview } from '../api/types';

export function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

export function renderApp(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <MemoryRouter initialEntries={[path]}>
          <AppRoutes />
          <LocationProbe />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

type Handler = (url: URL) => unknown;

/** Replace fetch with a router over path patterns; records every requested URL. */
export function mockApi(routes: Record<string, Handler>) {
  const calls: URL[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://127.0.0.1');
    calls.push(url);
    for (const [pattern, handler] of Object.entries(routes)) {
      if (new RegExp(`^${pattern}$`).test(url.pathname)) {
        return new Response(JSON.stringify(handler(url)), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }
    return new Response(JSON.stringify({ detail: 'There is no such thing.' }), { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

export const row: PageRow = {
  page_key: 'abc123',
  url: 'https://example.com/pricing',
  final_url: 'https://example.com/pricing',
  title: 'Pricing plans',
  host: 'example.com',
  badge: 'PARTIAL',
  words: 120,
  issues: 3,
  issue_keys: ['UNVISITED_STATE', 'UNSUPPORTED_FORMAT'],
  content_type: 'html',
  page_modified_at: '2026-10-01T00:00:00Z',
  captured_at: '2026-10-19T10:00:00Z',
  change_status: null,
  retryable: false,
};

export const pagesResponse: PagesResponse = {
  total: 1,
  page: 1,
  size: 50,
  items: [row],
  facets: {
    status: { PARTIAL: 1, VERIFIED_WITHIN_SCOPE: 0 },
    hosts: [{ value: 'example.com', count: 1 }],
    formats: [{ value: 'html', count: 1 }],
    issues: [{ value: 'UNVISITED_STATE', count: 1 }],
  },
  running: 0,
};

const block = (id: string, text: string, region: string, extra: Partial<PageDetail['blocks'][number]> = {}) => ({
  id,
  text,
  heading_path: ['Pricing plans'],
  order: Number(id.replace(/\D/g, '')),
  layer: 'rendered_visible',
  role: region === 'footer' ? 'footer' : 'main',
  region,
  format: 'html',
  verified: true,
  status: 'VERIFIED_EXTRACTED',
  confidence: 1,
  page_label: null,
  frame: 'main',
  ...extra,
});

export const detail: PageDetail = {
  row,
  run_name: 'demo',
  requested_url: row.url,
  aliases: [],
  page_modified_source: 'http:last-modified',
  page_published_at: null,
  change_basis: null,
  previous_run_id: null,
  reused_from_run_id: null,
  tier_reached: 'T3_FIDELITY_BROWSER',
  sniffed_mime: 'text/html',
  http_last_modified: null,
  followups: [],
  status_vector: { acquisition: 'VERIFIED_EXTRACTED' },
  inventory: {},
  discovery: {},
  warnings: ['frames_from_source_dom_only'],
  gaps: [
    {
      gap_id: 'g1',
      gap_type: 'UNVISITED_STATE',
      reason: 'unvisited_tab:policy_disallows:hover_info:Pricing',
      retry_eligible: false,
      status: 'PARTIAL',
      tier_reached: null,
    },
    {
      gap_id: 'g2',
      gap_type: 'UNSUPPORTED_FORMAT',
      reason: 'ocr_engine_missing:tesseract;image_content_unextracted',
      retry_eligible: false,
      status: 'UNSUPPORTED',
      tier_reached: null,
    },
  ],
  findings: [{ key: 'UNCONFIRMED_TEXT', unconfirmed: 2, total: 4 }],
  blocks: [
    block('b1', 'Pricing plans', 'main_content'),
    block('b2', 'Basic plan costs $10 <script>alert(1)</script>', 'main_content'),
    block('b3', 'Privacy policy and footer links', 'footer'),
    block('b4', 'Screen reader only note', 'main_content', { layer: 'accessibility_only', verified: false }),
  ],
  repeated_blocks: 1,
};

export const overview: Overview = {
  summary: {
    name: 'demo',
    run_id: 'run-1',
    records_file: 'demo.records.jsonl',
    page_count: 1,
    hosts: ['example.com'],
    badges: { PARTIAL: 1 },
    fully_captured_pct: 0,
    issue_count: 3,
    pages_with_issues: 1,
    captured_from: null,
    captured_to: null,
    file_modified_at: '2026-10-19T10:00:00Z',
    has_reports: false,
    running: 0,
    unreadable_lines: 0,
  },
  issues: [],
  formats: [],
  hosts: [],
  changes: [],
  tiers: [],
  words_total: 120,
  facts: null,
};
