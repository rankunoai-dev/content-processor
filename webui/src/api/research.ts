/**
 * Research module: a project's search data (keyword rankings per website, demand per
 * theme, movers) from a Semrush export import or a capped Semrush pull. Importing or
 * pulling needs the project's unlock token; pulling also needs Semrush switched on.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from './client';

export interface KeywordRow {
  keyword: string;
  position: number;
  previous_position: number | null;
  volume: number;
  cpc: number;
  competition: number | null;
  difficulty: number | null;
  url: string | null;
  traffic_share: number;
  theme_id: string | null;
}

export interface WebsiteResearch {
  website: string;
  name: string;
  role: 'client' | 'competitor';
  source: 'semrush_api' | 'csv_import';
  pulled_at: string;
  overview: { organic_keywords: number; organic_traffic: number; organic_cost: number; rank: number | null } | null;
  keywords: KeywordRow[];
  note: string;
}

export interface KeywordMetrics {
  keyword: string;
  volume: number;
  cpc: number;
  competition: number | null;
  results: number | null;
  serp: { rank: number; domain: string; url: string }[];
  theme_id: string | null;
}

export interface ThemeDemand {
  theme_id: string | null;
  label: string;
  keywords: number;
  volume: number;
  top10: Record<string, number>;
  ranked: Record<string, number>;
  traffic: Record<string, number>;
}

export interface Mover {
  website: string;
  keyword: string;
  before: number | null;
  after: number | null;
  volume: number;
  url: string | null;
}

export interface ResearchSnapshot {
  snapshot_id: string;
  project_id: string;
  created_at: string;
  database: string;
  websites: WebsiteResearch[];
  keywords: KeywordMetrics[];
  theme_map_id: string | null;
  demand: ThemeDemand[];
  units_spent: number;
  notes: string[];
}

export interface ResearchStatus {
  enabled: boolean;
  api_key_set: boolean;
  monthly_cap: number;
  used_this_month: number;
  pull_cap: number;
  keyword_limit: number;
  database: string;
}

export interface Estimate {
  websites: number;
  keyword_limit: number;
  worst_case_units: number;
  pull_cap: number;
}

const enc = encodeURIComponent;
const path = (projectId: string) => `/api/projects/${enc(projectId)}/research`;

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

/** A JSON request; a POST carries the write header and the unlock token. */
export async function moduleRequest<T>(url: string, init?: RequestInit, token?: string | null): Promise<T> {
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

/** Null instead of an error when the server says there is nothing yet. */
export async function orNull<T>(load: Promise<T>, notYet: string): Promise<T | null> {
  try {
    return await load;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && error.message.startsWith(notYet)) return null;
    throw error;
  }
}

/** The file's bytes as base64, for the JSON import body. */
export function fileBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result);
      resolve(url.slice(url.indexOf(',') + 1));
    };
    reader.onerror = () => reject(new ApiError(0, 'The file could not be read.'));
    reader.readAsDataURL(file);
  });
}

export const researchApi = {
  status: (signal?: AbortSignal) => moduleRequest<ResearchStatus>('/api/research/status', { signal }),
  latest: (projectId: string, signal?: AbortSignal) =>
    orNull(moduleRequest<ResearchSnapshot>(path(projectId), { signal }), 'No research'),
  estimate: (projectId: string, keywords: number, signal?: AbortSignal) =>
    moduleRequest<Estimate>(`${path(projectId)}/estimate?keywords=${keywords}`, { signal }),
  movers: (projectId: string, signal?: AbortSignal) =>
    orNull(moduleRequest<Mover[]>(`${path(projectId)}/movers`, { signal }), 'Add research again'),
  pull: (projectId: string, keywords: string[], token: string | null) =>
    moduleRequest<ResearchSnapshot>(
      `${path(projectId)}/pull`,
      { method: 'POST', body: JSON.stringify({ keywords }) },
      token,
    ),
  importFile: async (projectId: string, website: string, file: File, token: string | null) =>
    moduleRequest<ResearchSnapshot>(
      `${path(projectId)}/import`,
      {
        method: 'POST',
        body: JSON.stringify({ website, file_name: file.name, content_base64: await fileBase64(file) }),
      },
      token,
    ),
  workbookUrl: (projectId: string) => `${path(projectId)}/workbook`,
};

export const researchKeys = {
  status: ['research', 'status'] as const,
  latest: (projectId: string) => ['research', projectId] as const,
  estimate: (projectId: string, keywords: number) => ['research', projectId, 'estimate', keywords] as const,
  movers: (projectId: string) => ['research', projectId, 'movers'] as const,
};

export function useResearchStatus() {
  return useQuery({ queryKey: researchKeys.status, queryFn: ({ signal }) => researchApi.status(signal) });
}

export function useResearch(projectId: string) {
  return useQuery({
    queryKey: researchKeys.latest(projectId),
    queryFn: ({ signal }) => researchApi.latest(projectId, signal),
  });
}

export function useEstimate(projectId: string, keywords: number, enabled: boolean) {
  return useQuery({
    queryKey: researchKeys.estimate(projectId, keywords),
    queryFn: ({ signal }) => researchApi.estimate(projectId, keywords, signal),
    enabled,
  });
}

export function useMovers(projectId: string, enabled: boolean) {
  return useQuery({
    queryKey: researchKeys.movers(projectId),
    queryFn: ({ signal }) => researchApi.movers(projectId, signal),
    enabled,
  });
}

function useSaved(projectId: string) {
  const client = useQueryClient();
  return (snapshot: ResearchSnapshot) => {
    client.setQueryData(researchKeys.latest(projectId), snapshot);
    void client.invalidateQueries({ queryKey: researchKeys.movers(projectId) });
    void client.invalidateQueries({ queryKey: researchKeys.status });
  };
}

export function usePull(projectId: string) {
  const saved = useSaved(projectId);
  return useMutation({
    mutationFn: (args: { keywords: string[]; token: string | null }) =>
      researchApi.pull(projectId, args.keywords, args.token),
    onSuccess: saved,
  });
}

export function useImport(projectId: string) {
  const saved = useSaved(projectId);
  return useMutation({
    mutationFn: (args: { website: string; file: File; token: string | null }) =>
      researchApi.importFile(projectId, args.website, args.file, args.token),
    onSuccess: saved,
  });
}
