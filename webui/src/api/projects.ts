/**
 * App module 1 (Content Scraper): project API calls, types and React Query hooks.
 *
 * A project holds the client's website and its competitors' websites; each website
 * has its own URL list, scrapes, reports and knowledge space. Reading is open.
 * Every change carries the project's unlock token (from creating the project or
 * from unlocking it with the password); tokens live in memory only.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';
import { ApiError } from './client';

/** ready: its turn came in the shared queue and it waits for its person to start it. */
export type RunStatus = 'queued' | 'ready' | 'running' | 'finished' | 'failed' | 'cancelled' | 'interrupted';
export type KnowledgeStatus = '' | 'building' | 'ready' | 'failed' | 'skipped';

export interface RunState {
  run_id: string;
  project_id: string;
  site_id: string;
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
  knowledge_status: KnowledgeStatus;
  knowledge_message: string;
  /** auto: starts by itself when its turn comes; notify: email, then wait to be started. */
  on_turn: 'auto' | 'notify';
  email_when_done: boolean;
  /** The email address, partly hidden ('g***@rankuno.com'). */
  notify_to: string;
  /** Started after its turn came: runs before the waiting line. */
  jump: boolean;
  turn_at: string | null;
}

/** How a scrape waits for its turn: one scrape runs at a time on the whole app. */
export interface StartOptions {
  on_turn: 'auto' | 'notify';
  email: string | null;
  email_when_done: boolean;
}

export interface QueueItem {
  run: RunState;
  project_name: string;
  site_name: string;
  /** Place in the waiting line (1 runs next); 0 when running or waiting for its person. */
  position: number;
  /** Rough wait before it starts, in seconds. */
  wait_s: number;
}

export interface SharedQueue {
  running: QueueItem[];
  waiting: QueueItem[];
  ready: QueueItem[];
  seconds_per_url: number;
  /** False: emails are saved on the server, not sent (no mail server set up). */
  email_sends: boolean;
  /** A scrape whose turn came is cancelled when not started within these hours. */
  ready_hours: number;
}

export interface Site {
  site_id: string;
  name: string;
  /** client: one of the client's websites (never a competitor of the others); competitor. */
  role: 'client' | 'competitor';
  /** True for the client's main website. */
  main: boolean;
  domains: string[];
  /** What this website is, as the project team describes it. */
  note: string;
  tags: string[];
  url_count: number;
  last_run: RunState | null;
  namespace: string;
}

export interface Project {
  project_id: string;
  name: string;
  owner_name: string;
  client_name: string;
  created_at: string;
  updated_at: string;
  url_count: number;
  sites: Site[];
  last_run: RunState | null;
}

export interface Unlocked {
  project: Project;
  token: string;
  expires_at: string;
}

export interface ImportResult {
  site_id: string;
  site_name: string;
  added: number;
  already_present: number;
  duplicates_in_input: number;
  rejected: number;
  rejected_examples: { value: string; reason: string }[];
  over_limit: number;
  url_count: number;
}

export interface RoutedImport {
  sites: ImportResult[];
  unknown_domains: Record<string, number>;
  rejected: { value: string; reason: string }[];
}

export interface ProjectUrl {
  url: string;
  added_at: string;
}

/** A website and the scrape a project report reads for it. */
export interface ReportSource {
  site_id: string;
  site_name: string;
  role: string;
  main: boolean;
  run_id: string | null;
  finished_at: string | null;
  pages: number;
  reason: string;
}

/** One built project report (all of the client's websites). */
export interface ProjectReport {
  stamp: string;
  built_at: string;
  include_competitors: boolean;
  pages: number;
  contradictions: number;
  contradictions_high: number;
  differs_by_condition: number;
  time_bound: number;
  duplicate_pairs: number;
  duplicate_pairs_across_websites: number;
  websites: ReportSource[];
  files: ReportFile[];
}

export interface ProjectReports {
  sources: ReportSource[];
  reports: ProjectReport[];
  /** A build runs in the background (large websites take minutes). */
  building: boolean;
  building_since: string | null;
  /** Why the last build failed ('' when it did not). */
  last_error: string;
}

export interface ReportFile {
  name: string;
  size: number;
  modified_at: string;
}

export interface SearchHit {
  rank: number;
  score: number;
  website: string;
  project_id: string;
  project_name: string;
  site_id: string;
  site_name: string;
  role: string;
  url: string;
  headings: string[];
  text: string;
  chunk_id: string;
  evidence_ids: string[];
}

export interface SearchResult {
  query: string;
  scope: string[];
  hits: SearchHit[];
  searched: string[];
  skipped: { space: string; site: string; reason: string }[];
  /** Websites searched by exact words only (the meaning model was not reachable). */
  notes?: string[];
}

export interface NewProject {
  name: string;
  owner_name: string;
  client_name: string;
  password: string;
  client_website: string | null;
  client_note?: string;
  /** The client's other websites or brands: never its competitors. */
  client_brands?: SiteInput[];
  competitors: SiteInput[];
}

/** A website as entered in a form. */
export interface SiteInput {
  name: string;
  website: string | null;
  note?: string;
  tags?: string[];
  role?: 'client' | 'competitor';
}

/** Changes to a website; fields left out stay as they are. */
export interface SiteChange {
  name?: string;
  note?: string;
  tags?: string[];
  role?: 'client' | 'competitor';
}

export const ACTIVE: ReadonlySet<RunStatus> = new Set(['queued', 'ready', 'running']);

// -- unlock tokens --------------------------------------------------------------
// Kept in this browser tab's session storage: a reload or an app restart keeps the
// project unlocked until its token expires (8 hours); closing the tab locks every
// project again, and other tabs never see it. Falls back to memory when the browser
// refuses storage.

interface TokenState {
  tokens: Record<string, { token: string; expiresAt: number }>;
  remember: (projectId: string, token: string, expiresAt: string) => void;
  forget: (projectId: string) => void;
}

function tabStorage(): Storage {
  try {
    const probe = '__unlock_probe__';
    window.sessionStorage.setItem(probe, '1');
    window.sessionStorage.removeItem(probe);
    return window.sessionStorage;
  } catch {
    const memory = new Map<string, string>();
    return {
      get length() {
        return memory.size;
      },
      clear: () => memory.clear(),
      getItem: (k) => memory.get(k) ?? null,
      key: (i) => [...memory.keys()][i] ?? null,
      removeItem: (k) => void memory.delete(k),
      setItem: (k, v) => void memory.set(k, v),
    };
  }
}

/** Only the tokens that have not expired. */
function unexpired(tokens: TokenState['tokens']): TokenState['tokens'] {
  const now = Date.now();
  return Object.fromEntries(Object.entries(tokens).filter(([, t]) => t.expiresAt > now));
}

export const useProjectTokens = create<TokenState>()(
  persist(
    (set) => ({
      tokens: {},
      remember: (projectId, token, expiresAt) =>
        set((s) => ({
          tokens: unexpired({ ...s.tokens, [projectId]: { token, expiresAt: Date.parse(expiresAt) } }),
        })),
      forget: (projectId) =>
        set((s) => {
          const tokens = { ...s.tokens };
          delete tokens[projectId];
          return { tokens };
        }),
    }),
    {
      name: 'capture-results-unlocked',
      storage: createJSONStorage(tabStorage),
      partialize: (s) => ({ tokens: unexpired(s.tokens) }),
      merge: (stored, current) => ({
        ...current,
        tokens: unexpired((stored as Partial<TokenState> | undefined)?.tokens ?? {}),
      }),
    },
  ),
);

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

const projectPath = (id: string) => `/api/projects/${enc(id)}`;
const sitePath = (id: string, site: string) => `${projectPath(id)}/sites/${enc(site)}`;
const runPath = (id: string, site: string, run: string) => `${sitePath(id, site)}/runs/${enc(run)}`;

export const projectsApi = {
  list: (signal?: AbortSignal) => get<Project[]>('/api/projects', signal),
  get: (id: string, signal?: AbortSignal) => get<Project>(projectPath(id), signal),
  create: (body: NewProject) => post<Unlocked>('/api/projects', body),
  unlock: (id: string, password: string) => post<Unlocked>(`${projectPath(id)}/unlock`, { password }),
  remove: (id: string, password: string, token: string | null) =>
    post<{ deleted: string }>(`${projectPath(id)}/delete`, { password }, token),

  addSite: (id: string, site: SiteInput, token: string | null) =>
    post<Site>(`${projectPath(id)}/sites`, site, token),
  updateSite: (id: string, site: string, change: SiteChange, token: string | null) =>
    post<Site>(`${sitePath(id, site)}/update`, change, token),
  removeSite: (id: string, site: string, token: string | null) =>
    post<{ deleted: string }>(`${sitePath(id, site)}/delete`, {}, token),

  /** One list for every website: each URL goes to the website of its domain. */
  addUrlsAnySite: (id: string, text: string, token: string | null) =>
    post<RoutedImport>(`${projectPath(id)}/urls`, { text }, token),
  importAnySite: (id: string, fileName: string, contentBase64: string, token: string | null) =>
    post<RoutedImport>(
      `${projectPath(id)}/urls/import`,
      { file_name: fileName, content_base64: contentBase64 },
      token,
    ),
  urls: (id: string, site: string, q: string, offset: number, limit: number, signal?: AbortSignal) =>
    get<{ total: number; items: ProjectUrl[] }>(
      `${sitePath(id, site)}/urls?${new URLSearchParams({ q, offset: String(offset), limit: String(limit) }).toString()}`,
      signal,
    ),
  addUrls: (id: string, site: string, text: string, token: string | null) =>
    post<ImportResult>(`${sitePath(id, site)}/urls`, { text }, token),
  importFile: (id: string, site: string, fileName: string, contentBase64: string, token: string | null) =>
    post<ImportResult>(
      `${sitePath(id, site)}/urls/import`,
      { file_name: fileName, content_base64: contentBase64 },
      token,
    ),
  removeUrls: (id: string, site: string, urls: string[] | 'all', token: string | null) =>
    post<{ removed: number; url_count: number }>(
      `${sitePath(id, site)}/urls/remove`,
      urls === 'all' ? { all: true } : { urls },
      token,
    ),

  runs: (id: string, signal?: AbortSignal) => get<RunState[]>(`${projectPath(id)}/runs`, signal),
  startAll: (id: string, options: StartOptions, token: string | null) =>
    post<RunState[]>(`${projectPath(id)}/runs`, options, token),
  start: (id: string, site: string, options: StartOptions, token: string | null) =>
    post<RunState>(`${sitePath(id, site)}/runs`, options, token),
  /** Start a scrape whose turn came: it runs next, before the waiting line. */
  startReady: (id: string, site: string, run: string, token: string | null) =>
    post<RunState>(`${runPath(id, site, run)}/start`, {}, token),
  queue: (signal?: AbortSignal) => get<SharedQueue>('/api/queue', signal),
  cancel: (id: string, site: string, run: string, token: string | null) =>
    post<RunState>(`${runPath(id, site, run)}/cancel`, {}, token),
  /** Continue a stopped scrape: nothing already saved is requested again. */
  resume: (id: string, site: string, run: string, token: string | null) =>
    post<RunState>(`${runPath(id, site, run)}/resume`, {}, token),
  reports: (id: string, site: string, run: string, signal?: AbortSignal) =>
    get<ReportFile[]>(`${runPath(id, site, run)}/downloads`, signal),
  reportUrl: (id: string, site: string, run: string, file: string) =>
    `${runPath(id, site, run)}/downloads/${enc(file)}`,
  zipUrl: (id: string, site: string, run: string) => `${runPath(id, site, run)}/downloads.zip`,

  /** Duplication and contradictions across all of the client's websites. */
  projectReports: (id: string, signal?: AbortSignal) => get<ProjectReports>(`${projectPath(id)}/reports`, signal),
  buildProjectReports: (id: string, includeCompetitors: boolean, token: string | null) =>
    post<ProjectReports>(`${projectPath(id)}/reports`, { include_competitors: includeCompetitors }, token),
  projectReportUrl: (id: string, stamp: string, file: string) =>
    `${projectPath(id)}/reports/${enc(stamp)}/downloads/${enc(file)}`,
  projectZipUrl: (id: string, stamp: string) => `${projectPath(id)}/reports/${enc(stamp)}/downloads.zip`,

  /** Search the given spaces, or (with `website`) that website and its competitors from every project. */
  search: (q: string, spaces: string[], perSite: number | null, website: string | null, signal?: AbortSignal) => {
    const params = new URLSearchParams({ q, top_k: '12' });
    if (website) params.set('website', website);
    else for (const s of spaces) params.append('space', s);
    if (perSite !== null) params.set('per_site', String(perSite));
    return get<SearchResult>(`/api/knowledge/search?${params.toString()}`, signal);
  },
};

// -- hooks --------------------------------------------------------------------------

export const projectKeys = {
  all: () => ['projects'] as const,
  one: (id: string) => ['projects', id] as const,
  urls: (id: string, site: string, q: string, offset: number, limit: number) =>
    ['projects', id, 'urls', site, q, offset, limit] as const,
  runs: (id: string) => ['projects', id, 'runs'] as const,
  reports: (id: string, site: string, run: string) => ['projects', id, 'reports', site, run] as const,
  projectReports: (id: string) => ['projects', id, 'project-reports'] as const,
  search: (q: string, spaces: string[], perSite: number | null, website: string | null) =>
    ['knowledge', 'search', q, spaces, perSite, website] as const,
};

const POLL_MS = 3000;

/** The shared queue of scrapes across every project; refreshed every few seconds. */
export function useSharedQueue() {
  return useQuery({
    queryKey: ['queue'] as const,
    queryFn: ({ signal }) => projectsApi.queue(signal),
    refetchInterval: POLL_MS,
  });
}

export function useProjects() {
  return useQuery({
    queryKey: projectKeys.all(),
    queryFn: ({ signal }) => projectsApi.list(signal),
    refetchInterval: (q) =>
      q.state.data?.some((p) => p.sites.some((s) => s.last_run && ACTIVE.has(s.last_run.status)))
        ? POLL_MS
        : false,
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: projectKeys.one(id),
    queryFn: ({ signal }) => projectsApi.get(id, signal),
    refetchInterval: (q) =>
      q.state.data?.sites.some((s) => s.last_run && ACTIVE.has(s.last_run.status)) ? POLL_MS : false,
  });
}

export function useSiteUrls(id: string, site: string, q: string, offset: number, limit: number) {
  return useQuery({
    queryKey: projectKeys.urls(id, site, q, offset, limit),
    queryFn: ({ signal }) => projectsApi.urls(id, site, q, offset, limit, signal),
    placeholderData: (previous) => previous,
  });
}

/** Every run of the project (all websites); polled while one is queued or running. */
export function useProjectRuns(id: string) {
  return useQuery({
    queryKey: projectKeys.runs(id),
    queryFn: ({ signal }) => projectsApi.runs(id, signal),
    refetchInterval: (q) => (q.state.data?.some((r) => ACTIVE.has(r.status)) ? POLL_MS : false),
  });
}

export function useRunReports(id: string, site: string, run: string | null) {
  return useQuery({
    queryKey: projectKeys.reports(id, site, run ?? ''),
    queryFn: ({ signal }) => projectsApi.reports(id, site, run ?? '', signal),
    enabled: run !== null,
  });
}

export function useProjectReports(id: string) {
  return useQuery({
    queryKey: projectKeys.projectReports(id),
    queryFn: ({ signal }) => projectsApi.projectReports(id, signal),
    refetchInterval: (q) => (q.state.data?.building ? POLL_MS : false),
  });
}

export function useKnowledgeSearch(
  q: string,
  spaces: string[],
  perSite: number | null,
  website: string | null,
) {
  return useQuery({
    queryKey: projectKeys.search(q, spaces, perSite, website),
    queryFn: ({ signal }) => projectsApi.search(q, spaces, perSite, website, signal),
    enabled: q.trim().length >= 2 && (website !== null || spaces.length > 0),
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
