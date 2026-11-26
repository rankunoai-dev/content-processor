import { App as AntApp } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ConnectionsResponse } from '../api/connections';
import { ConnectionsPage } from '../routes/ConnectionsPage';
import { mockApi } from './utils';

const LISTING: ConnectionsResponse = {
  platforms: ['semrush', 'ahrefs', 'gsc', 'ga4'],
  connections: [
    {
      connection_id: 'agency-semrush',
      platform: 'semrush',
      label: 'Agency Semrush',
      secret_env: 'SEMRUSH_AGENCY',
      key_set: false,
      property_ref: '',
      created_by: 'gaurav',
      created_at: '2026-11-26T09:00:00Z',
      used_by: ['Life cover'],
    },
  ],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AntApp>
        <ConnectionsPage />
      </AntApp>
    </QueryClientProvider>,
  );
}

describe('Connections page', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('lists each saved connection by its key variable name and whether the key is set', async () => {
    mockApi({ '/api/connections': () => LISTING });
    renderPage();
    expect(await screen.findByText('Agency Semrush')).toBeTruthy();
    expect(screen.getByText('SEMRUSH_AGENCY')).toBeTruthy();
    expect(screen.getByText('Key not set')).toBeTruthy();
    expect(screen.getByText('Life cover')).toBeTruthy();
  });

  it('never shows a key value (the listing carries none)', async () => {
    mockApi({ '/api/connections': () => LISTING });
    renderPage();
    await screen.findByText('Agency Semrush');
    expect(document.body.textContent ?? '').not.toMatch(/sk-|secret-value/i);
  });
});
