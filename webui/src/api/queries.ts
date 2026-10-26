import {
  QueryClient,
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import { useCallback } from 'react';
import { ApiError, api, type PageFilters } from './client';
import type { DecisionIn } from './types';

/** Every query key in one place, so invalidation and prefetching agree. */
export const queryKeys = {
  runs: () => ['runs'] as const,
  run: (run: string) => ['run', run] as const,
  overview: (run: string) => ['run', run, 'overview'] as const,
  pages: (run: string, filters: PageFilters) => ['run', run, 'pages', filters] as const,
  page: (run: string, key: string) => ['run', run, 'page', key] as const,
  pageRaw: (run: string, key: string) => ['run', run, 'page', key, 'raw'] as const,
  duplicates: (run: string) => ['run', run, 'duplicates'] as const,
  contradictions: (run: string) => ['run', run, 'contradictions'] as const,
  review: (run: string) => ['run', run, 'review'] as const,
  downloads: (run: string) => ['run', run, 'downloads'] as const,
  contentTypes: () => ['content-types'] as const,
  contentTypeScope: (types: string[]) => ['content-types', 'scope', types] as const,
};

const POLL_MS = 5000;

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 10 * 60_000,
        refetchOnWindowFocus: false,
        // A 4xx (unknown run or page) will not fix itself; network and 5xx get two retries.
        retry: (count, error) =>
          !(error instanceof ApiError && error.isClientError) && count < 2,
      },
    },
  });
}

/**
 * Poll a run only while some of its pages are still being captured. The
 * overview query carries the RUNNING count, so every run-scoped query asks it.
 */
function useRunPolling(run: string): number | false {
  const client = useQueryClient();
  const overview = client.getQueryData<{ summary: { running: number } }>(queryKeys.overview(run));
  return overview && overview.summary.running > 0 ? POLL_MS : false;
}

export function useContentTypes() {
  return useQuery({
    queryKey: queryKeys.contentTypes(),
    queryFn: ({ signal }) => api.contentTypes(signal),
    staleTime: Infinity,
  });
}

export function useContentTypeScope(types: string[]) {
  return useQuery({
    queryKey: queryKeys.contentTypeScope(types),
    queryFn: ({ signal }) => api.contentTypeScope(types, signal),
    enabled: types.length > 0,
  });
}

export function useRuns() {
  return useQuery({
    queryKey: queryKeys.runs(),
    queryFn: ({ signal }) => api.runs(signal),
    refetchInterval: (query) =>
      query.state.data?.runs.some((r) => r.running > 0) ? POLL_MS : false,
  });
}

export function useOverview(run: string) {
  return useQuery({
    queryKey: queryKeys.overview(run),
    queryFn: ({ signal }) => api.overview(run, signal),
    refetchInterval: (query) => ((query.state.data?.summary.running ?? 0) > 0 ? POLL_MS : false),
  });
}

export function usePages(run: string, filters: PageFilters) {
  return useQuery({
    queryKey: queryKeys.pages(run, filters),
    queryFn: ({ signal }) => api.pages(run, filters, signal),
    placeholderData: keepPreviousData,
    refetchInterval: (query) => ((query.state.data?.running ?? 0) > 0 ? POLL_MS : false),
  });
}

export function usePage(run: string, key: string) {
  const poll = useRunPolling(run);
  return useQuery({
    queryKey: queryKeys.page(run, key),
    queryFn: ({ signal }) => api.page(run, key, signal),
    refetchInterval: (query) => (query.state.data?.row.badge === 'RUNNING' ? POLL_MS : poll),
  });
}

export function usePageRaw(run: string, key: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.pageRaw(run, key),
    queryFn: ({ signal }) => api.pageRaw(run, key, signal),
    enabled,
  });
}

export function useDuplicates(run: string) {
  return useQuery({
    queryKey: queryKeys.duplicates(run),
    queryFn: ({ signal }) => api.duplicates(run, signal),
    refetchInterval: useRunPolling(run),
  });
}

export function useContradictions(run: string) {
  return useQuery({
    queryKey: queryKeys.contradictions(run),
    queryFn: ({ signal }) => api.contradictions(run, signal),
    refetchInterval: useRunPolling(run),
  });
}

export function useReview(run: string) {
  return useQuery({
    queryKey: queryKeys.review(run),
    queryFn: ({ signal }) => api.review(run, signal),
    refetchInterval: useRunPolling(run),
  });
}

/** Save a reviewer decision, then refresh the review and contradiction lists. */
export function useSaveDecision(run: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (decision: DecisionIn) => api.saveDecision(run, decision),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.contradictions(run) }),
        client.invalidateQueries({ queryKey: queryKeys.review(run) }),
      ]);
    },
  });
}

export function useDownloads(run: string) {
  return useQuery({
    queryKey: queryKeys.downloads(run),
    queryFn: ({ signal }) => api.downloads(run, signal),
  });
}

/** Warm the page detail cache when the pointer rests on a row or link. */
export function usePrefetchPage(run: string) {
  const client = useQueryClient();
  return useCallback(
    (key: string) => {
      void client.prefetchQuery({
        queryKey: queryKeys.page(run, key),
        queryFn: ({ signal }) => api.page(run, key, signal),
        staleTime: 60_000,
      });
    },
    [client, run],
  );
}
