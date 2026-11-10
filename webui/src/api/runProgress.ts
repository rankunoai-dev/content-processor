/**
 * Live progress of one scrape: its batches, the pages of a batch and the batch log.
 *
 * Read-only, open like the other project reads. The server reads the run's folder
 * (state, scope files, saved captures, records, logs) and keeps every answer small:
 * batches and pages come 100 at a time, the log 200 lines at a time from a byte offset.
 */
import { useQuery } from '@tanstack/react-query';
import { ApiError } from './client';

export type BatchState = 'waiting' | 'running' | 'done' | 'retrying';
export type UrlStage = 'queued' | 'downloaded' | 'rendered' | 'done';

/** How far the attempt running now has come, from the captures saved so far. */
export interface InProgress {
  pages: number;
  fetched: number;
  rendered: number;
}

export interface BatchProgress {
  number: number;
  state: BatchState;
  attempts: number;
  attempt: number | null;
  pages: number;
  pages_finished: number;
  by_badge: Record<string, number>;
  started_at: string | null;
  finished_at: string | null;
  in_progress: InProgress | null;
}

export interface BatchesPage {
  run_id: string;
  status: string;
  batch_size: number;
  total: number;
  offset: number;
  current_batch: number | null;
  items: BatchProgress[];
}

export interface UrlRow {
  url: string;
  stage: UrlStage;
  badge: string | null;
  reason: string;
  /** Percent of main-content blocks whose text was verified; null without a result or main content. */
  completeness: number | null;
  gaps: number;
  attempt: number | null;
}

export interface UrlsPage {
  batch: number;
  total: number;
  offset: number;
  items: UrlRow[];
}

export interface LogLine {
  time: string;
  /** INFO, WARNING, ERROR ...; TEXT for a plain line such as a traceback. */
  level: string;
  message: string;
  extra: Record<string, unknown>;
}

export interface LogChunk {
  batch: number | null;
  attempt: number | null;
  exists: boolean;
  /** Pass back as `after` to get the next lines. */
  offset: number;
  size: number;
  lines: LogLine[];
}

export const BATCHES_PER_PAGE = 100;
export const URLS_PER_PAGE = 100;
export const ACTIVE_POLL_MS = 3000;
export const IDLE_POLL_MS = 15000;
export const LOG_POLL_MS = 2000;
/** Lines kept in the browser for one log; older ones scroll off. */
export const LOG_MAX_LINES = 2000;

const enc = encodeURIComponent;

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal, headers: { Accept: 'application/json' } });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Could not reach the app server. Is it still running?');
  }
  if (!response.ok) {
    let message = `The server answered with an error (${response.status}).`;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === 'object' && 'detail' in body) {
        const detail = (body as { detail: unknown }).detail;
        if (typeof detail === 'string') message = detail;
      }
    } catch {
      // not JSON
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

const runPath = (id: string, site: string, run: string) =>
  `/api/projects/${enc(id)}/sites/${enc(site)}/runs/${enc(run)}`;

export const runProgressApi = {
  batches: (id: string, site: string, run: string, offset: number, signal?: AbortSignal) =>
    get<BatchesPage>(
      `${runPath(id, site, run)}/batches?${new URLSearchParams({ offset: String(offset), limit: String(BATCHES_PER_PAGE) }).toString()}`,
      signal,
    ),
  urls: (id: string, site: string, run: string, batch: number, offset: number, signal?: AbortSignal) =>
    get<UrlsPage>(
      `${runPath(id, site, run)}/batches/${batch}/urls?${new URLSearchParams({ offset: String(offset), limit: String(URLS_PER_PAGE) }).toString()}`,
      signal,
    ),
  log: (id: string, site: string, run: string, batch: number, attempt: number, after: number, signal?: AbortSignal) =>
    get<LogChunk>(
      `${runPath(id, site, run)}/log?${new URLSearchParams({ batch: String(batch), attempt: String(attempt), after: String(after) }).toString()}`,
      signal,
    ),
};

export const progressKeys = {
  batches: (id: string, site: string, run: string, page: number) =>
    ['projects', id, 'progress', site, run, 'batches', page] as const,
  urls: (id: string, site: string, run: string, batch: number, page: number) =>
    ['projects', id, 'progress', site, run, 'urls', batch, page] as const,
};

/** A page of the run's batches; polled every 3 s while the scrape is active, 15 s otherwise. */
export function useRunBatches(id: string, site: string, run: string, page: number, active: boolean) {
  return useQuery({
    queryKey: progressKeys.batches(id, site, run, page),
    queryFn: ({ signal }) => runProgressApi.batches(id, site, run, (page - 1) * BATCHES_PER_PAGE, signal),
    refetchInterval: active ? ACTIVE_POLL_MS : IDLE_POLL_MS,
    placeholderData: (previous) => previous,
  });
}

/** The pages of one batch, on demand; polled only while that batch is being scraped. */
export function useBatchUrls(id: string, site: string, run: string, batch: number | null, page: number, live: boolean) {
  return useQuery({
    queryKey: progressKeys.urls(id, site, run, batch ?? 0, page),
    queryFn: ({ signal }) => runProgressApi.urls(id, site, run, batch ?? 0, (page - 1) * URLS_PER_PAGE, signal),
    enabled: batch !== null,
    refetchInterval: live ? ACTIVE_POLL_MS : false,
    placeholderData: (previous) => previous,
  });
}
