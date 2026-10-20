import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { detail, mockApi, overview, renderApp } from './utils';

afterEach(() => vi.unstubAllGlobals());

function setup(path = '/runs/demo/pages/abc123') {
  const calls = mockApi({
    '/api/runs': () => ({ exports_dir: 'x', runs: [] }),
    '/api/runs/demo/overview': () => overview,
    '/api/runs/demo/pages/abc123': () => detail,
    '/api/runs/demo/pages/abc123/raw': () => ({ requested_url: detail.requested_url }),
  });
  renderApp(path);
  return calls;
}

describe('page detail', () => {
  it('shows the outcome and main content first, menus and footer hidden', async () => {
    setup();
    expect(await screen.findByRole('heading', { name: 'Pricing plans', level: 2 })).toBeInTheDocument();
    expect(screen.getByText(/We captured 120 words, but 3 issues/)).toBeInTheDocument();
    expect(screen.getByText('Basic plan costs $10 <script>alert(1)</script>')).toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
    expect(screen.queryByText('Privacy policy and footer links')).not.toBeInTheDocument();
    expect(screen.queryByText('Screen reader only note')).not.toBeInTheDocument();
  });

  it('turns page areas and hidden text on through the URL', async () => {
    setup();
    await screen.findByRole('heading', { name: 'Pricing plans', level: 2 });
    await userEvent.click(screen.getByText('Footer'));
    expect(await screen.findByText('Privacy policy and footer links')).toBeInTheDocument();
    expect(screen.getByTestId('location').textContent).toContain('areas=main%2Cfooter');
    await userEvent.click(screen.getByRole('switch', { name: 'Include hidden text' }));
    expect(await screen.findByText('Screen reader only note')).toBeInTheDocument();
  });

  it('explains issues in plain words, raw reasons only under technical details', async () => {
    setup('/runs/demo/pages/abc123?tab=issues');
    const tabpanel = await screen.findByRole('tabpanel');
    expect(within(tabpanel).getByText("A tab, menu or button wasn't opened")).toBeInTheDocument();
    expect(
      within(tabpanel).getByText(/The "Pricing" tab wasn't opened because it needs hovering, which is turned off for this run/),
    ).toBeInTheDocument();
    expect(within(tabpanel).getByText("File type we can't read yet")).toBeInTheDocument();
    expect(within(tabpanel).getByText(/2 of 4 pieces of text could not be double-checked/)).toBeInTheDocument();
    expect(within(tabpanel).queryByText(/unvisited_tab:policy_disallows/)).not.toBeInTheDocument();
    for (const toggle of within(tabpanel).getAllByText('Technical details')) await userEvent.click(toggle);
    await waitFor(() => expect(within(tabpanel).getByText(/unvisited_tab:policy_disallows/)).toBeInTheDocument());
  });

  it('switches tabs and keeps the tab in the URL', async () => {
    setup();
    await screen.findByRole('heading', { name: 'Pricing plans', level: 2 });
    await userEvent.click(screen.getByRole('tab', { name: 'Page facts' }));
    expect(await screen.findByText('Real browser')).toBeInTheDocument();
    expect(screen.getByText(/Embedded frames were read from the page code only/)).toBeInTheDocument();
    expect(screen.getByTestId('location').textContent).toContain('tab=facts');
    expect(screen.queryByRole('tab', { name: 'Technical' })).not.toBeInTheDocument();
  });

  it('shows an error with the server sentence for an unknown page', async () => {
    mockApi({ '/api/runs/demo/overview': () => overview });
    renderApp('/runs/demo/pages/nope');
    expect(await screen.findByText('There is no such thing.')).toBeInTheDocument();
  });
});
