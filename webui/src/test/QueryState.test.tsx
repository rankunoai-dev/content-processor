import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { QueryState, type QueryStateProps } from '../components/QueryState';

type Q = QueryStateProps<string[]>['query'];

function q(over: Partial<Q>): Q {
  return {
    data: undefined,
    error: null,
    isPending: false,
    isError: false,
    isFetching: false,
    refetch: vi.fn(),
    ...over,
  } as Q;
}

const child = (data: string[]) => <ul>{data.map((d) => <li key={d}>{d}</li>)}</ul>;

describe('QueryState', () => {
  it('shows a skeleton while loading', () => {
    render(<QueryState query={q({ isPending: true })}>{child}</QueryState>);
    expect(screen.getByRole('status', { name: 'Loading' })).toBeInTheDocument();
  });

  it('shows the API message and retries on request', async () => {
    const refetch = vi.fn();
    render(
      <QueryState query={q({ isError: true, error: new ApiError(500, 'The disk is full.'), refetch })}>{child}</QueryState>,
    );
    expect(screen.getByText("We couldn't load this")).toBeInTheDocument();
    expect(screen.getByText('The disk is full.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(refetch).toHaveBeenCalled();
  });

  it('does not offer a retry for a missing item', () => {
    render(<QueryState query={q({ isError: true, error: new ApiError(404, 'No such run.') })}>{child}</QueryState>);
    expect(screen.getByText('No such run.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
  });

  it('shows the empty sentence when there is nothing', () => {
    render(
      <QueryState query={q({ data: [] })} isEmpty={(d) => d.length === 0} empty="Nothing here yet. Run a capture first.">
        {child}
      </QueryState>,
    );
    expect(screen.getByText('Nothing here yet. Run a capture first.')).toBeInTheDocument();
  });

  it('renders the data', () => {
    render(<QueryState query={q({ data: ['one', 'two'] })} isEmpty={(d) => d.length === 0}>{child}</QueryState>);
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });
});
