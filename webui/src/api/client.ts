import type {
  ContradictionsResponse,
  DecisionIn,
  DecisionOut,
  DownloadsResponse,
  DuplicatesResponse,
  Overview,
  PageDetail,
  PagesResponse,
  ReviewResponse,
  RunsResponse,
} from './types';

/** An API failure with the server's own sentence, when it sent one. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }

  /** 4xx answers will not change on retry. */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500;
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal, headers: { Accept: 'application/json' } });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Could not reach the results server. Is it still running?');
  }
  if (!response.ok) {
    let message = `The server answered with an error (${response.status}).`;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === 'object' && 'detail' in body) {
        const detail = (body as { detail: unknown }).detail;
        if (typeof detail === 'string') message = detail;
        else if (Array.isArray(detail)) message = 'Some filter values were not understood.';
      }
    } catch {
      // not JSON: keep the generic sentence
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

/** The only write: saving a reviewer decision. The header proves it comes from this app. */
async function postJson<T>(path: string, body: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-Requested-With': 'content-processor',
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, 'Could not reach the results server. Is it still running?');
  }
  if (!response.ok) {
    let message = `The decision was not saved (${response.status}).`;
    try {
      const data: unknown = await response.json();
      if (data && typeof data === 'object' && 'detail' in data) {
        const detail = (data as { detail: unknown }).detail;
        if (typeof detail === 'string') message = detail;
      }
    } catch {
      // not JSON: keep the generic sentence
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

export interface PageFilters {
  status: string[];
  host: string[];
  format: string[];
  issue: string[];
  hasIssues: boolean | null;
  q: string;
  sort: string;
  order: 'asc' | 'desc';
  page: number;
  size: number;
}

export function pagesQueryString(f: PageFilters): string {
  const p = new URLSearchParams();
  for (const s of f.status) p.append('status', s);
  for (const h of f.host) p.append('host', h);
  for (const t of f.format) p.append('format', t);
  for (const i of f.issue) p.append('issue', i);
  if (f.hasIssues !== null) p.set('has_issues', String(f.hasIssues));
  if (f.q.trim()) p.set('q', f.q.trim());
  p.set('sort', f.sort);
  p.set('order', f.order);
  p.set('page', String(f.page));
  p.set('size', String(f.size));
  return p.toString();
}

const enc = encodeURIComponent;

export const api = {
  runs: (signal?: AbortSignal) => getJson<RunsResponse>('/api/runs', signal),
  overview: (run: string, signal?: AbortSignal) =>
    getJson<Overview>(`/api/runs/${enc(run)}/overview`, signal),
  pages: (run: string, filters: PageFilters, signal?: AbortSignal) =>
    getJson<PagesResponse>(`/api/runs/${enc(run)}/pages?${pagesQueryString(filters)}`, signal),
  page: (run: string, key: string, signal?: AbortSignal) =>
    getJson<PageDetail>(`/api/runs/${enc(run)}/pages/${enc(key)}`, signal),
  pageRaw: (run: string, key: string, signal?: AbortSignal) =>
    getJson<Record<string, unknown>>(`/api/runs/${enc(run)}/pages/${enc(key)}/raw`, signal),
  duplicates: (run: string, signal?: AbortSignal) =>
    getJson<DuplicatesResponse>(`/api/runs/${enc(run)}/duplicates`, signal),
  contradictions: (run: string, signal?: AbortSignal) =>
    getJson<ContradictionsResponse>(`/api/runs/${enc(run)}/contradictions`, signal),
  review: (run: string, signal?: AbortSignal) =>
    getJson<ReviewResponse>(`/api/runs/${enc(run)}/review`, signal),
  downloads: (run: string, signal?: AbortSignal) =>
    getJson<DownloadsResponse>(`/api/runs/${enc(run)}/downloads`, signal),
  downloadUrl: (run: string, file: string) => `/api/runs/${enc(run)}/downloads/${enc(file)}`,
  saveDecision: (run: string, decision: DecisionIn) =>
    postJson<DecisionOut>(`/api/runs/${enc(run)}/decisions`, decision),
};
