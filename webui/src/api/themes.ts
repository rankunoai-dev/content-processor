/**
 * Theming module: a project's theme map (topic clusters, pillars, ownership, funnel).
 * Reading is open; building a new map needs the project's unlock token.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from './client';

export type Ownership = 'client_owned' | 'contested' | 'competitor_led' | 'client_gap' | 'client_only';
export type FunnelStage = 'learn' | 'compare' | 'decide' | 'onboard' | 'support' | 'unknown';

export interface ThemeWebsite {
  website: string;
  name: string;
  role: 'client' | 'competitor';
  project: string;
  run_id: string;
  pages: number;
}

export interface Pillar {
  url: string;
  title: string | null;
  score: number;
  reasons: string[];
}

export interface SiteCoverage {
  website: string;
  pages: number;
  words: number;
  share: number;
  pillar: Pillar | null;
}

export interface Topic {
  topic_id: string;
  label: string;
  terms: string[];
  pages: string[];
  websites: Record<string, number>;
}

export interface Theme {
  theme_id: string;
  label: string;
  terms: string[];
  pages: number;
  coverage: SiteCoverage[];
  ownership: Ownership;
  ownership_reason: string;
  topics: Topic[];
  funnel: Record<string, number>;
  offerings: string[];
}

export interface PageTheme {
  url: string;
  website: string;
  title: string | null;
  theme_id: string | null;
  topic_id: string | null;
  role: 'pillar' | 'spoke' | 'unclustered';
  similarity: number;
  funnel_stage: FunnelStage;
  funnel_reason: string;
  page_type: string | null;
  words: number;
}

export interface ThemeMap {
  map_id: string;
  project_id: string;
  built_at: string;
  method: string;
  websites: ThemeWebsite[];
  themes: Theme[];
  pages: PageTheme[];
  skipped: string[];
  stats: Record<string, number>;
}

const enc = encodeURIComponent;
const path = (projectId: string) => `/api/projects/${enc(projectId)}/themes`;

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

/** The latest map, or null when none has been built yet. */
async function latest(projectId: string, signal?: AbortSignal): Promise<ThemeMap | null> {
  let response: Response;
  try {
    response = await fetch(path(projectId), { signal, headers: { Accept: 'application/json' } });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Could not reach the app server. Is it still running?');
  }
  if (response.status === 404) {
    const text = await detail(response, '');
    if (text.startsWith('No theme map')) return null;
    throw new ApiError(404, text || 'There is no such project.');
  }
  if (!response.ok) {
    throw new ApiError(response.status, await detail(response, `The server answered with an error (${response.status}).`));
  }
  return (await response.json()) as ThemeMap;
}

async function build(projectId: string, token: string | null): Promise<ThemeMap> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'X-Requested-With': 'content-processor',
  };
  if (token) headers['X-Project-Token'] = token;
  let response: Response;
  try {
    response = await fetch(path(projectId), { method: 'POST', headers, body: '{}' });
  } catch {
    throw new ApiError(0, 'Could not reach the app server. Is it still running?');
  }
  if (!response.ok) {
    throw new ApiError(response.status, await detail(response, `The theme map could not be built (${response.status}).`));
  }
  return (await response.json()) as ThemeMap;
}

export const themesApi = {
  latest,
  build,
  workbookUrl: (projectId: string) => `${path(projectId)}/workbook`,
};

export const themeKeys = { latest: (projectId: string) => ['themes', projectId] as const };

export function useThemeMap(projectId: string) {
  return useQuery({
    queryKey: themeKeys.latest(projectId),
    queryFn: ({ signal }) => themesApi.latest(projectId, signal),
  });
}

export function useBuildThemes(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (token: string | null) => themesApi.build(projectId, token),
    onSuccess: (map) => client.setQueryData(themeKeys.latest(projectId), map),
  });
}
