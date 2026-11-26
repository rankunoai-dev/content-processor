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
  /** By rules: informational, navigational, commercial or transactional. */
  intent?: string | null;
  /** Other pages of the same website ranking for it (cannibalisation). */
  other_urls?: string[];
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
  /** Monthly searches per intent. */
  by_intent?: Record<string, number>;
  /** Searches weighted by intent (commercial 1, informational 0.5). */
  weighted_volume?: number;
}

export interface PageRow {
  url: string;
  title: string | null;
  theme_id: string | null;
  theme: string | null;
  keywords: number;
  volume: number;
  visits: number;
  average_position: number | null;
  best_position: number | null;
  top_keyword: string | null;
  commercial_volume: number;
  inbound_body_links: number;
  inbound_chrome_links: number;
  pagerank: number;
  word_count: number | null;
  flags: string[];
  gsc_clicks: number | null;
  gsc_impressions: number | null;
  gsc_ctr: number | null;
  gsc_position: number | null;
  ga_sessions?: number | null;
  ga_organic_sessions?: number | null;
  ga_engagement_rate?: number | null;
  ga_conversions?: number | null;
  ga_ai_sessions?: number | null;
  indexable: boolean;
}

export interface Cannibalisation {
  keyword: string;
  volume: number;
  intent: string | null;
  pages: string[];
  positions: number[];
  source: 'semrush_rows' | 'url_swap' | 'search_console';
  note: string;
}

export interface LinkSuggestion {
  source_url: string;
  source_title: string | null;
  source_pagerank: number;
  target_url: string;
  target_title: string | null;
  anchor: string;
  keyword_position: number;
  keyword_volume: number;
  theme: string | null;
  reason: string;
}

export interface CtrOpportunity {
  kind: 'page' | 'query';
  label: string;
  impressions: number;
  clicks: number;
  ctr: number;
  expected_ctr: number;
  position: number;
  extra_clicks: number;
}

export interface SearchLine {
  query: string;
  page: string;
  weight: number;
  clicks: number | null;
  position: number | null;
  funnel: 'tofu' | 'mofu' | 'bofu';
  topic: string | null;
  owner: string | null;
  status: 'aligned' | 'also_aligned' | 'misaligned' | 'no_page' | 'unplaced';
  why: string;
}

export interface PageAlignment {
  url: string;
  page_class: 'business' | 'article' | 'utility';
  weight: number;
  aligned: number;
  share: number;
  status: 'strong' | 'within_tolerance' | 'needs_work' | 'too_little_data';
  misaligned_to: Record<string, number>;
}

export interface ConsolidationPair {
  group: number;
  page: string;
  owner: string;
  topic: string | null;
  pattern: string;
  fix: string;
  weight: number;
  priority: 'High' | 'Medium' | 'Low';
  searches: SearchLine[];
  shared_paragraphs: number;
}

export interface PlanLinkChange {
  kind: 'repoint' | 'fix_anchor' | 'add_link';
  group: number | null;
  source: string;
  destination: string;
  new_destination: string | null;
  anchor: string;
  suggested_anchor: string;
  reason: string;
}

export interface ConsolidationPlan {
  source: 'search_console' | 'search_data' | 'none';
  scored_pages: number;
  tolerance: number;
  alignment: PageAlignment[];
  pairs: ConsolidationPair[];
  links: PlanLinkChange[];
  technical: { kind: string; group: number | null; url: string; target: string | null; reason: string }[];
  template_paragraphs: number;
  set_aside: Record<string, number>;
}

export interface PageReport {
  project_id: string;
  website: string;
  built_at: string;
  research_snapshot_id: string | null;
  gsc_period: string | null;
  pages: PageRow[];
  cannibalisation: Cannibalisation[];
  zero_traffic: string[];
  orphans: string[];
  near_orphans: string[];
  stuck: string[];
  link_suggestions: LinkSuggestion[];
  ctr_opportunities: CtrOpportunity[];
  /** The keyword cannibalisation plan (absent without search data or Search Console). */
  plan?: ConsolidationPlan | null;
  notes: string[];
}

export interface GscSummary {
  website: string;
  imported_at: string;
  period: string;
  queries: number;
  pages: number;
  pairs: number;
  clicks: number;
  impressions: number;
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
  min_interval_ms?: number;
  fresh_days?: number;
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
  pull: (projectId: string, keywords: string[], token: string | null, refresh = false) =>
    moduleRequest<ResearchSnapshot>(
      `${path(projectId)}/pull`,
      { method: 'POST', body: JSON.stringify(refresh ? { keywords, refresh } : { keywords }) },
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
  pages: (projectId: string, signal?: AbortSignal) =>
    orNull(moduleRequest<PageReport>(`${path(projectId)}/pages`, { signal }), 'No page report'),
  pagesWorkbookUrl: (projectId: string) => `${path(projectId)}/pages/workbook`,
  gsc: (projectId: string, signal?: AbortSignal) =>
    orNull(moduleRequest<GscSummary>(`${path(projectId)}/gsc`, { signal }), 'No Search Console'),
  importGsc: async (projectId: string, website: string, file: File, token: string | null) =>
    moduleRequest<GscSummary>(
      `${path(projectId)}/gsc/import`,
      {
        method: 'POST',
        body: JSON.stringify({ website, file_name: file.name, content_base64: await fileBase64(file) }),
      },
      token,
    ),
};

export const researchKeys = {
  status: ['research', 'status'] as const,
  latest: (projectId: string) => ['research', projectId] as const,
  estimate: (projectId: string, keywords: number) => ['research', projectId, 'estimate', keywords] as const,
  movers: (projectId: string) => ['research', projectId, 'movers'] as const,
  pages: (projectId: string) => ['research', projectId, 'pages'] as const,
  gsc: (projectId: string) => ['research', projectId, 'gsc'] as const,
};

export function usePageReport(projectId: string, enabled: boolean) {
  return useQuery({
    queryKey: researchKeys.pages(projectId),
    queryFn: ({ signal }) => researchApi.pages(projectId, signal),
    enabled,
  });
}

export function useGsc(projectId: string) {
  return useQuery({
    queryKey: researchKeys.gsc(projectId),
    queryFn: ({ signal }) => researchApi.gsc(projectId, signal),
  });
}

export function useImportGsc(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { website: string; file: File; token: string | null }) =>
      researchApi.importGsc(projectId, args.website, args.file, args.token),
    onSuccess: (summary: GscSummary) => {
      client.setQueryData(researchKeys.gsc(projectId), summary);
      void client.invalidateQueries({ queryKey: researchKeys.pages(projectId) });
    },
  });
}

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
    void client.invalidateQueries({ queryKey: researchKeys.pages(projectId) });
    void client.invalidateQueries({ queryKey: researchKeys.status });
    void client.invalidateQueries({ queryKey: ['research', projectId, 'competitors'] });
  };
}

export function usePull(projectId: string) {
  const saved = useSaved(projectId);
  return useMutation({
    mutationFn: (args: { keywords: string[]; token: string | null; refresh?: boolean }) =>
      researchApi.pull(projectId, args.keywords, args.token, args.refresh ?? false),
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

// -- suggested competitors -------------------------------------------------------------------

export type SuggestionSource = 'semrush_competitors' | 'competitors_export' | 'search_results';

export interface CompetitorSuggestion {
  domain: string;
  status: 'suggested' | 'added' | 'dismissed';
  sources: SuggestionSource[];
  relevance: number | null;
  common_keywords: number;
  organic_keywords: number;
  organic_traffic: number;
  search_results: number;
  example_keywords: string[];
  first_seen: string;
  last_seen: string;
  times_seen: number;
  decided_at: string | null;
  site_id: string | null;
}

export interface CompetitorBook {
  project_id: string;
  updated_at: string;
  client_domain: string | null;
  suggestions: CompetitorSuggestion[];
}

export const competitorsApi = {
  book: (projectId: string, signal?: AbortSignal) =>
    moduleRequest<CompetitorBook>(`${path(projectId)}/competitors`, { signal }),
  importFile: async (projectId: string, file: File, token: string | null) =>
    moduleRequest<CompetitorBook>(
      `${path(projectId)}/competitors/import`,
      { method: 'POST', body: JSON.stringify({ file_name: file.name, content_base64: await fileBase64(file) }) },
      token,
    ),
  decide: (projectId: string, domain: string, action: 'add' | 'dismiss' | 'restore', token: string | null) =>
    moduleRequest<CompetitorBook>(
      `${path(projectId)}/competitors/${enc(domain)}/${action}`,
      { method: 'POST', body: '{}' },
      token,
    ),
};

export const competitorKeys = {
  book: (projectId: string) => ['research', projectId, 'competitors'] as const,
};

export function useCompetitorBook(projectId: string) {
  return useQuery({
    queryKey: competitorKeys.book(projectId),
    queryFn: ({ signal }) => competitorsApi.book(projectId, signal),
  });
}

function useBookSaved(projectId: string) {
  const client = useQueryClient();
  return (book: CompetitorBook) => {
    client.setQueryData(competitorKeys.book(projectId), book);
    // adding a competitor changes the project's websites
    void client.invalidateQueries({ queryKey: ['projects'] });
  };
}

export function useImportCompetitors(projectId: string) {
  const saved = useBookSaved(projectId);
  return useMutation({
    mutationFn: (args: { file: File; token: string | null }) =>
      competitorsApi.importFile(projectId, args.file, args.token),
    onSuccess: saved,
  });
}

export function useDecideCompetitor(projectId: string) {
  const saved = useBookSaved(projectId);
  return useMutation({
    mutationFn: (args: { domain: string; action: 'add' | 'dismiss' | 'restore'; token: string | null }) =>
      competitorsApi.decide(projectId, args.domain, args.action, args.token),
    onSuccess: saved,
  });
}
