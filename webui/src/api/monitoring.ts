/**
 * Monitoring module: a project's latest check (changes, price shifts, theme drift,
 * alerts) and how the project is watched. Running a check or changing the watch
 * settings needs the project's unlock token.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from './client';

export type Severity = 'high' | 'medium' | 'low' | 'info';

export interface PageRef {
  url: string;
  title: string | null;
  theme_id: string | null;
  theme: string | null;
}

export interface PriceChange {
  before: string;
  after: string;
  context: string;
}

export interface PageChange {
  url: string;
  title: string | null;
  theme: string | null;
  head_changes: Record<string, { before: string | null; after: string | null }>;
  sections_added: number;
  sections_removed: number;
  added_text: string[];
  removed_text: string[];
  drift: number;
  prices: PriceChange[];
  new_numbers: string[];
  is_pillar: boolean;
  words_before: number;
  words_after: number;
  headings_added: string[];
  headings_removed: string[];
  new_terms: string[];
  dropped_terms: string[];
  blocks_added: Record<string, number>;
  blocks_removed: Record<string, number>;
}

export interface TemplateChange {
  line: string;
  added_on: number;
  removed_on: number;
}

export interface SiteDiff {
  website: string;
  name: string;
  role: 'client' | 'competitor';
  baseline_run: string | null;
  latest_run: string | null;
  baseline_at: string | null;
  latest_at: string | null;
  note: string;
  new_pages: PageRef[];
  removed_pages: PageRef[];
  changed_pages: PageChange[];
  unchanged_pages: number;
  template_changes: TemplateChange[];
  drift: number;
}

export interface ThemeDrift {
  theme_id: string;
  label: string;
  status: 'new' | 'gone' | 'existing';
  ownership_before: string | null;
  ownership_after: string | null;
  new_pages: Record<string, number>;
  removed_pages: Record<string, number>;
  pages_before: Record<string, number>;
  pages_after: Record<string, number>;
}

export interface Alert {
  alert_id: string;
  severity: Severity;
  kind: string;
  website: string;
  title: string;
  detail: string;
  theme_id: string | null;
  urls: string[];
}

export interface MonitorCheck {
  check_id: string;
  project_id: string;
  created_at: string;
  trigger: 'manual' | 'auto';
  websites: SiteDiff[];
  theme_drift: ThemeDrift[];
  alerts: Alert[];
  stats: Record<string, number>;
}

export interface WatchSettings {
  auto_check: boolean;
  rescrape_every_days: number;
  push_threshold: number;
  smart_recrawl: boolean;
  other_every_days: number;
  use_sitemaps: boolean;
  core_urls: string[];
  core_limit: number;
  last_rescrape_at: string | null;
  last_auto_check_at: string | null;
}

export type WatchBody = Pick<WatchSettings, 'auto_check' | 'rescrape_every_days' | 'push_threshold'> &
  Partial<Pick<WatchSettings, 'smart_recrawl' | 'other_every_days' | 'use_sitemaps'>>;

export interface PageDecision {
  url: string;
  tier: 'core' | 'other';
  action: 'check' | 'skip';
  reason: string;
  sitemap_lastmod: string | null;
  last_captured: string | null;
}

export interface SitePlan {
  site_id: string;
  name: string;
  role: 'client' | 'competitor';
  website: string;
  baseline_run: string | null;
  sitemap_files: string[];
  sitemap_addresses: number;
  sitemap_dates: number;
  sitemap_note: string;
  core: number;
  check: number;
  skip: number;
  run_id: string | null;
  note: string;
  pages: PageDecision[];
}

export interface RecrawlPlan {
  plan_id: string;
  project_id: string;
  created_at: string;
  trigger: string;
  core_every_days: number;
  other_every_days: number;
  sites: SitePlan[];
}

const enc = encodeURIComponent;
const path = (projectId: string) => `/api/projects/${enc(projectId)}/monitoring`;

async function detail(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail;
      if (typeof d === 'string') return d;
    }
  } catch {
    // not JSON
  }
  return fallback;
}

async function request<T>(url: string, init?: RequestInit, token?: string | null): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (init?.method === 'POST') {
    headers['Content-Type'] = 'application/json';
    headers['X-Requested-With'] = 'content-processor';
    if (token) headers['X-Project-Token'] = token;
  }
  let response: Response;
  try {
    response = await fetch(url, { ...init, headers });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Could not reach the app server. Is it still running?');
  }
  if (!response.ok) {
    throw new ApiError(response.status, await detail(response, `The server answered with an error (${response.status}).`));
  }
  return (await response.json()) as T;
}

/** The latest check, or null when none has been run. */
async function latest(projectId: string, signal?: AbortSignal): Promise<MonitorCheck | null> {
  try {
    return await request<MonitorCheck>(path(projectId), { signal });
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && error.message.startsWith('No monitoring check')) {
      return null;
    }
    throw error;
  }
}

export const monitoringApi = {
  latest,
  run: (projectId: string, token: string | null) =>
    request<MonitorCheck>(path(projectId), { method: 'POST', body: '{}' }, token),
  watch: (projectId: string, signal?: AbortSignal) => request<WatchSettings>(`${path(projectId)}/watch`, { signal }),
  setWatch: (projectId: string, body: WatchBody, token: string | null) =>
    request<WatchSettings>(`${path(projectId)}/watch`, { method: 'POST', body: JSON.stringify(body) }, token),
  plan: async (projectId: string, signal?: AbortSignal): Promise<RecrawlPlan | null> => {
    try {
      return await request<RecrawlPlan>(`${path(projectId)}/recrawl`, { signal });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  },
  makePlan: (projectId: string, queue: boolean, token: string | null) =>
    request<RecrawlPlan>(`${path(projectId)}/recrawl`, { method: 'POST', body: JSON.stringify({ queue }) }, token),
  workbookUrl: (projectId: string) => `${path(projectId)}/workbook`,
};

export const monitoringKeys = {
  latest: (projectId: string) => ['monitoring', projectId] as const,
  watch: (projectId: string) => ['monitoring', projectId, 'watch'] as const,
  plan: (projectId: string) => ['monitoring', projectId, 'recrawl'] as const,
};

export function useRecrawlPlan(projectId: string) {
  return useQuery({
    queryKey: monitoringKeys.plan(projectId),
    queryFn: ({ signal }) => monitoringApi.plan(projectId, signal),
  });
}

export function useMakePlan(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { queue: boolean; token: string | null }) =>
      monitoringApi.makePlan(projectId, args.queue, args.token),
    onSuccess: (plan) => {
      client.setQueryData(monitoringKeys.plan(projectId), plan);
      void client.invalidateQueries({ queryKey: ['exports', projectId] });
      if (plan.trigger !== 'preview') void client.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useMonitorCheck(projectId: string) {
  return useQuery({
    queryKey: monitoringKeys.latest(projectId),
    queryFn: ({ signal }) => monitoringApi.latest(projectId, signal),
  });
}

export function useWatchSettings(projectId: string) {
  return useQuery({
    queryKey: monitoringKeys.watch(projectId),
    queryFn: ({ signal }) => monitoringApi.watch(projectId, signal),
  });
}

export function useRunCheck(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (token: string | null) => monitoringApi.run(projectId, token),
    onSuccess: (check) => client.setQueryData(monitoringKeys.latest(projectId), check),
  });
}

export function useSetWatch(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { body: WatchBody; token: string | null }) =>
      monitoringApi.setWatch(projectId, args.body, args.token),
    onSuccess: (settings) => client.setQueryData(monitoringKeys.watch(projectId), settings),
  });
}
