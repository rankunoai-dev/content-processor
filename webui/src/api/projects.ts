/**
 * App module 1 (Content Scraper): project API calls, types and React Query hooks.
 *
 * Reading is open. Every change carries the project's unlock token (from creating
 * the project or from unlocking it with the password); tokens live in memory only.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { create } from 'zustand';
import { ApiError } from './client';

export type RunStatus = 'queued' | 'running' | 'finished' | 'failed' | 'cancelled' | 'interrupted';

export interface RunState {
  run_id: string;
  project_id: string;
  status: RunStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  started_by: string;
  url_count: number;
  batch_size: number;
  batches_total: number;
  batches_done: number;
  current_batch: number | null;
  current_attempt: number | null;
  pages_done: number;
  results: Record<string, number>;
  message: string;
  reports_ready: boolean;
}

export interface Project {
  project_id: string;
  name: string;
  owner_name: string;
  client_name: string;
  created_at: string;
  updated_at: string;
  url_count: number;
  last_run: RunState | null;
}

export interface Unlocked {
  project: Project;
  token: string;
  expires_at: string;
}

export interface ImportResult {
  added: number;
  already_present: number;
  duplicates_in_input: number;
  rejected: number;
  rejected_examples: { value: string; reason: string }[];
  over_limit: number;
  url_count: number;
}

export interface ProjectUrl {
  url: string;
  added_at: string;
}

export interface ReportFile {
  name: string;
  size: number;
  modified_at: string;
}

export const ACTIVE: ReadonlySet<RunStatus> = new Set(['queued', 'running']);

// -- unlock tokens (memory only: closing the tab locks every project again) --------

interface TokenState {
  tokens: Record<string, { token: string; expiresAt: number }>;
  remember: (projectId: string, token: string, expiresAt: string) => void;
  forget: (projectId: string) => void;
}

export const useProjectTokens = create<TokenState>((set) => ({
  tokens: {},
  remember: (projectId, token, expiresAt) =>
    set((s) => ({
      tokens: { ...s.tokens, [projectId]: { token, expiresAt: Date.parse(expiresAt) } },
    })),
  forget: (projectId) =>
    set((s) => {
      const tokens = { ...s.tokens };
      delete tokens[projectId];
      return { tokens };
    }),
}));

/** The project's token while it is valid, else null. */
export function useProjectToken(projectId: string): string | null {
  const entry = useProjectTokens((s) => s.tokens[projectId]);
  if (!entry || entry.expiresAt <= Date.now()) return null;
  return entry.token;
}

// -- calls --------------------------------------------------------------------------

const enc = encodeURIComponent;

async function detail(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail;
      if (typeof d === 'string') return d;
      if (Array.isArray(d) && d.length > 0) {
        const first = d[0] as { msg?: unknown; loc?: unknown };
        const field = Array.isArray(first.loc) ? String(first.loc[first.loc.length - 1]) : '';
        if (typeof first.msg === 'string') return field ? `${field}: ${first.msg}` : first.msg;
      }
    }
  } catch {
    // not JSON
  }
  return fallback;
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal, headers: { Accept: 'application/json' } });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Could not reach the app server. Is it still running?');
  }
  if (!response.ok) {
    throw new ApiError(response.status, await detail(response, `The server answered with an error (${response.status}).`));
  }
  return (await response.json()) as T;
}

/** A change: JSON, the app's own header, and the project's token when it has one. */
async function post<T>(path: string, body: unknown, token?: string | null): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'X-Requested-With': 'content-processor',
  };
  if (token) headers['X-Project-Token'] = token;
  let response: Response;
  try {
    response = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) });
  } catch {
    throw new ApiError(0, 'Could not reach the app server. Is it still running?');
  }
  if (!response.ok) {
    throw new ApiError(response.status, await detail(response, `Not saved (${response.status}).`));
  }
  return (await response.json()) as T;
}

export const projectsApi = {
  list: (signal?: AbortSignal) => get<Project[]>('/api/projects', signal),
  get: (id: string, signal?: AbortSignal) => get<Project>(`/api/projects/${enc(id)}`, signal),
  urls: (id: string, q: string, offset: number, limit: number, signal?: AbortSignal) =>
    get<{ total: number; items: ProjectUrl[] }>(
      `/api/projects/${enc(id)}/urls?${new URLSearchParams({ q, offset: String(offset), limit: String(limit) }).toString()}`,
      signal,
    ),
  runs: (id: string, signal?: AbortSignal) => get<RunState[]>(`/api/projects/${enc(id)}/runs`, signal),
  reports: (id: string, run: string, signal?: AbortSignal) =>
    get<ReportFile[]>(`/api/projects/${enc(id)}/runs/${enc(run)}/downloads`, signal),
  reportUrl: (id: string, run: string, file: string) =>
    `/api/projects/${enc(id)}/runs/${enc(run)}/downloads/${enc(file)}`,
  zipUrl: (id: string, run: string) => `/api/projects/${enc(id)}/runs/${enc(run)}/downloads.zip`,
  create: (body: { name: string; owner_name: string; client_name: string; password: string }) =>
    post<Unlocked>('/api/projects', body),
  unlock: (id: string, password: string) =>
    post<Unlocked>(`/api/projects/${enc(id)}/unlock`, { password }),
  addUrls: (id: string, text: string, token: string | null) =>
    post<ImportResult>(`/api/projects/${enc(id)}/urls`, { text }, token),
  importFile: (id: string, fileName: string, contentBase64: string, token: string | null) =>
    post<ImportResult>(
      `/api/projects/${enc(id)}/urls/import`,
      { file_name: fileName, content_base64: contentBase64 },
      token,
    ),
  removeUrls: (id: string, urls: string[] | 'all', token: string | null) =>
    post<{ removed: number; url_count: number }>(
      `/api/projects/${enc(id)}/urls/remove`,
      urls === 'all' ? { all: true } : { urls },
      token,
    ),
  start: (id: string, token: string | null) => post<RunState>(`/api/projects/${enc(id)}/runs`, {}, token),
  cancel: (id: string, run: string, token: string | null) =>
    post<RunState>(`/api/projects/${enc(id)}/runs/${enc(run)}/cancel`, {}, token),
  remove: (id: string, password: string, token: string | null) =>
    post<{ deleted: string }>(`/api/projects/${enc(id)}/delete`, { password }, token),
};

// -- hooks --------------------------------------------------------------------------

export const projectKeys = {
  all: () => ['projects'] as const,
  one: (id: string) => ['projects', id] as const,
  urls: (id: string, q: string, offset: number, limit: number) =>
    ['projects', id, 'urls', q, offset, limit] as const,
  runs: (id: string) => ['projects', id, 'runs'] as const,
  reports: (id: string, run: string) => ['projects', id, 'runs', run, 'reports'] as const,
};

const POLL_MS = 3000;

export function useProjects() {
  return useQuery({
    queryKey: projectKeys.all(),
    queryFn: ({ signal }) => projectsApi.list(signal),
    refetchInterval: (q) =>
      q.state.data?.some((p) => p.last_run && ACTIVE.has(p.last_run.status)) ? POLL_MS : false,
  });
}

export function useProject(id: string) {
  return useQuery({ queryKey: projectKeys.one(id), queryFn: ({ signal }) => projectsApi.get(id, signal) });
}

export function useProjectUrls(id: string, q: string, offset: number, limit: number) {
  return useQuery({
    queryKey: projectKeys.urls(id, q, offset, limit),
    queryFn: ({ signal }) => projectsApi.urls(id, q, offset, limit, signal),
    placeholderData: (previous) => previous,
  });
}

/** The project's runs; polled while one is queued or running. */
export function useProjectRuns(id: string) {
  return useQuery({
    queryKey: projectKeys.runs(id),
    queryFn: ({ signal }) => projectsApi.runs(id, signal),
    refetchInterval: (q) => (q.state.data?.some((r) => ACTIVE.has(r.status)) ? POLL_MS : false),
  });
}

export function useRunReports(id: string, run: string | null) {
  return useQuery({
    queryKey: projectKeys.reports(id, run ?? ''),
    queryFn: ({ signal }) => projectsApi.reports(id, run ?? '', signal),
    enabled: run !== null,
  });
}

/** Any project change; refreshes the project, its URLs and its runs afterwards. */
export function useProjectChange<TArgs, TResult>(id: string, fn: (args: TArgs) => Promise<TResult>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: projectKeys.one(id) }),
        client.invalidateQueries({ queryKey: ['projects', id, 'urls'] }),
        client.invalidateQueries({ queryKey: projectKeys.runs(id) }),
        client.invalidateQueries({ queryKey: projectKeys.all(), exact: true }),
      ]);
    },
  });
}

/** Read a file from the device as base64 (without the data: prefix). */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('The file could not be read.'));
    reader.onload = () => {
      const result = String(reader.result ?? '');
      resolve(result.slice(result.indexOf(',') + 1));
    };
    reader.readAsDataURL(file);
  });
}
