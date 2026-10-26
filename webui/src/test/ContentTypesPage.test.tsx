import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { ContentType, ScopeFragment } from '../api/types';
import { mockApi, renderApp } from './utils';

const TYPES: ContentType[] = [
  {
    id: 'html',
    label: 'Web pages',
    families: ['text/html'],
    extensions: ['.html'],
    status: 'supported',
    note: 'Always read.',
  },
  {
    id: 'pdf',
    label: 'PDF documents',
    families: ['application/pdf'],
    extensions: ['.pdf'],
    status: 'supported',
    note: 'Text and tables.',
  },
  {
    id: 'legacy_office',
    label: 'Older Office files (.doc, .xls, .ppt)',
    families: ['application/msword'],
    extensions: ['.doc'],
    status: 'not_supported',
    note: 'Needs LibreOffice.',
  },
];

function scopeFor(types: string[]): ScopeFragment {
  const unsupported = types.filter((t) => t === 'legacy_office');
  return {
    selected: types,
    allowed_mime_families: ['text/html', 'application/pdf', ...(types.includes('legacy_office') ? ['application/msword'] : [])],
    unsupported_selected: unsupported,
    command: 'python -m src.modules.content_processor.cli dryrun --scope scope.json --out out',
  };
}

describe('Content types page', () => {
  it('lists every type with its support status', async () => {
    mockApi({
      '/api/content-types': () => ({ types: TYPES }),
      '/api/content-types/scope': (url) => scopeFor(url.searchParams.get('types')!.split(',')),
    });
    renderApp('/content-types');
    expect(await screen.findByText('PDF documents')).toBeInTheDocument();
    expect(screen.getByText('Not supported yet')).toBeInTheDocument();
    expect(screen.getAllByText('Supported')).toHaveLength(2);
  });

  it('defaults to the supported types and shows the command without starting a run', async () => {
    const calls = mockApi({
      '/api/content-types': () => ({ types: TYPES }),
      '/api/content-types/scope': (url) => scopeFor(url.searchParams.get('types')!.split(',')),
    });
    renderApp('/content-types');
    expect(await screen.findByText(/cli dryrun --scope scope.json/)).toBeInTheDocument();
    const scopeCall = calls.find((u) => u.pathname === '/api/content-types/scope');
    expect(scopeCall?.searchParams.get('types')).toBe('html,pdf');
    expect(calls.some((u) => u.pathname.includes('/runs'))).toBe(false);
  });

  it('warns when an unsupported type is selected, and says it will be recorded as not supported', async () => {
    mockApi({
      '/api/content-types': () => ({ types: TYPES }),
      '/api/content-types/scope': (url) => scopeFor(url.searchParams.get('types')!.split(',')),
    });
    renderApp('/content-types');
    await screen.findByText('PDF documents');
    const option = screen.getByRole('checkbox', { name: /Older Office files/ });
    await userEvent.click(option);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/not extracted yet/)).toBeInTheDocument();
    expect(within(alert).getByText(/recorded as not supported/)).toBeInTheDocument();
  });

  it('clearing the selection asks for at least one type', async () => {
    mockApi({
      '/api/content-types': () => ({ types: TYPES }),
      '/api/content-types/scope': (url) => scopeFor(url.searchParams.get('types')!.split(',')),
    });
    renderApp('/content-types');
    await screen.findByText('PDF documents');
    await userEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
    await waitFor(() =>
      expect(screen.getByText(/Select at least one content type/)).toBeInTheDocument(),
    );
  });
});
