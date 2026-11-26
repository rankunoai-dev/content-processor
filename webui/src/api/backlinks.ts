/**
 * Backlinks module: a project's link data (Domain Rating, referring domains, top linked
 * pages, link intersect) from an Ahrefs export import or a capped Ahrefs pull. Importing
 * or pulling needs the project's unlock token; pulling also needs Ahrefs switched on.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fileBase64, moduleRequest, orNull } from './research';

export interface RefDomain {
  domain: string;
  domain_rating: number | null;
  dofollow_links: number;
  links_to_target: number;
  first_seen: string | null;
  last_seen: string | null;
  is_spam: boolean;
}

export interface TopPage {
  url: string;
  title: string | null;
  referring_domains: number;
  links: number;
  url_rating: number | null;
  http_code: number | null;
}

export interface WebsiteBacklinks {
  website: string;
  name: string;
  role: 'client' | 'competitor';
  source: 'ahrefs_api' | 'csv_import';
  pulled_at: string;
  domain_rating: number | null;
  ahrefs_rank: number | null;
  stats: { live: number; all_time: number; live_refdomains: number; all_time_refdomains: number } | null;
  refdomains: RefDomain[];
  refdomains_asked: number;
  top_pages: TopPage[];
  lost_domains: string[];
  new_domains: number | null;
  note: string;
}

export interface IntersectDomain {
  domain: string;
  domain_rating: number | null;
  competitors: string[];
  links: number;
}

export interface AuthorityGap {
  competitor: string;
  metric: 'domain_rating' | 'referring_domains';
  client: number;
  competitor_value: number;
  gap: number;
  client_refdomains: number;
  competitor_refdomains: number;
}

export interface BacklinkSnapshot {
  snapshot_id: string;
  project_id: string;
  created_at: string;
  websites: WebsiteBacklinks[];
  insights: {
    client: string | null;
    min_competitors: number;
    link_intersect: IntersectDomain[];
    authority: AuthorityGap[];
    client_top_pages: TopPage[];
    lost: Record<string, number>;
    notes: string[];
  };
  units_spent: number;
  notes: string[];
}

export interface BacklinksStatus {
  enabled: boolean;
  api_key_set: boolean;
  monthly_cap: number;
  used_this_month: number;
  pull_cap: number;
  refdomains_limit: number;
  pages_limit: number;
  min_interval_ms: number;
}

export interface BacklinksEstimate {
  websites: number;
  refdomains_limit: number;
  pages_limit: number;
  worst_case_units: number;
  pull_cap: number;
}

export type ExportKind = 'refdomains' | 'pages';

const enc = encodeURIComponent;
const path = (projectId: string) => `/api/projects/${enc(projectId)}/backlinks`;

export const backlinksApi = {
  status: (signal?: AbortSignal) => moduleRequest<BacklinksStatus>('/api/backlinks/status', { signal }),
  latest: (projectId: string, signal?: AbortSignal) =>
    orNull(moduleRequest<BacklinkSnapshot>(path(projectId), { signal }), 'No backlink data'),
  estimate: (projectId: string, signal?: AbortSignal) =>
    moduleRequest<BacklinksEstimate>(`${path(projectId)}/estimate`, { signal }),
  pull: (projectId: string, token: string | null) =>
    moduleRequest<BacklinkSnapshot>(`${path(projectId)}/pull`, { method: 'POST', body: '{}' }, token),
  importFile: async (projectId: string, website: string, kind: ExportKind, file: File, token: string | null) =>
    moduleRequest<BacklinkSnapshot>(
      `${path(projectId)}/import`,
      {
        method: 'POST',
        body: JSON.stringify({ website, kind, file_name: file.name, content_base64: await fileBase64(file) }),
      },
      token,
    ),
  workbookUrl: (projectId: string) => `${path(projectId)}/workbook`,
};

export const backlinksKeys = {
  status: ['backlinks', 'status'] as const,
  latest: (projectId: string) => ['backlinks', projectId] as const,
  estimate: (projectId: string) => ['backlinks', projectId, 'estimate'] as const,
};

export function useBacklinksStatus() {
  return useQuery({ queryKey: backlinksKeys.status, queryFn: ({ signal }) => backlinksApi.status(signal) });
}

export function useBacklinks(projectId: string) {
  return useQuery({
    queryKey: backlinksKeys.latest(projectId),
    queryFn: ({ signal }) => backlinksApi.latest(projectId, signal),
  });
}

export function useBacklinksEstimate(projectId: string, enabled: boolean) {
  return useQuery({
    queryKey: backlinksKeys.estimate(projectId),
    queryFn: ({ signal }) => backlinksApi.estimate(projectId, signal),
    enabled,
  });
}

function useSaved(projectId: string) {
  const client = useQueryClient();
  return (snapshot: BacklinkSnapshot) => {
    client.setQueryData(backlinksKeys.latest(projectId), snapshot);
    void client.invalidateQueries({ queryKey: backlinksKeys.status });
  };
}

export function useImportBacklinks(projectId: string) {
  const saved = useSaved(projectId);
  return useMutation({
    mutationFn: (args: { website: string; kind: ExportKind; file: File; token: string | null }) =>
      backlinksApi.importFile(projectId, args.website, args.kind, args.file, args.token),
    onSuccess: saved,
  });
}

export function usePullBacklinks(projectId: string) {
  const saved = useSaved(projectId);
  return useMutation({
    mutationFn: (args: { token: string | null }) => backlinksApi.pull(projectId, args.token),
    onSuccess: saved,
  });
}
