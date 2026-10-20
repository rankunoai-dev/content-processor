import { Button, Empty, Result, Skeleton } from 'antd';
import type { UseQueryResult } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { ApiError } from '../api/client';
import { APP } from '../vocabulary';

export interface QueryStateProps<T> {
  query: Pick<UseQueryResult<T>, 'data' | 'error' | 'isPending' | 'isError' | 'refetch' | 'isFetching'>;
  /** Treat the data as empty (shows the Empty state with `empty`). */
  isEmpty?: (data: T) => boolean;
  /** A helpful sentence: why it is empty and what to do. */
  empty?: ReactNode;
  /** Number of skeleton rows while loading. */
  rows?: number;
  children: (data: T) => ReactNode;
}

/**
 * The four states of every data view: loading, error (with retry), empty
 * (with a sentence) and data. Every screen renders its server data through it.
 */
export function QueryState<T>({ query, isEmpty, empty, rows = 6, children }: QueryStateProps<T>) {
  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" aria-label="Loading">
        <Skeleton active paragraph={{ rows }} />
      </div>
    );
  }
  if (query.isError || query.data === undefined) {
    const error = query.error;
    const notFound = error instanceof ApiError && error.status === 404;
    const message =
      error instanceof Error ? error.message : 'Something went wrong while loading.';
    return (
      <Result
        status={notFound ? '404' : 'error'}
        title={APP.loadErrorTitle}
        subTitle={message}
        extra={
          notFound ? null : (
            <Button type="primary" loading={query.isFetching} onClick={() => void query.refetch()}>
              {APP.retry}
            </Button>
          )
        }
      />
    );
  }
  if (isEmpty?.(query.data)) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={<span className="empty-text">{empty}</span>}
        style={{ padding: '32px 0' }}
      />
    );
  }
  return <>{children(query.data)}</>;
}
