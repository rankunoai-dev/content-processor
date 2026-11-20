/**
 * Competitor lite scan: read a big competitor's sitemap, scrape a capped sample of its
 * pages with plain downloads (no browser), and keep the rest of the sitemap as an
 * inventory the theme map and the gaps count as "inferred".
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { projectKeys, type RunState } from './projects';
import { moduleRequest, orNull } from './research';

export interface LitePick {
  url: string;
  reason: 'pinned' | 'ranking' | 'core' | 'section';
  detail: string;
  section: string;
}

export interface LiteSection {
  section: string;
  pages: number;
  sampled: number;
}

export interface LiteSummary {
  site_id: string;
  website: string;
  created_at: string;
  run_id: string;
  /** Pages the website publishes as far as we know (its sitemap). */
  inventory: number;
  sampled: number;
  max_pages: number;
  /** "based on 150 of about 5,000 pages (lite scan)" */
  basis: string;
  sitemap_files: number;
  sitemap_note: string;
  sections: LiteSection[];
  sample: LitePick[];
  run: RunState | null;
}

const enc = encodeURIComponent;
const path = (projectId: string, siteId: string) =>
  `/api/projects/${enc(projectId)}/sites/${enc(siteId)}/lite`;

export const liteApi = {
  latest: (projectId: string, siteId: string, signal?: AbortSignal) =>
    orNull(moduleRequest<LiteSummary>(path(projectId, siteId), { signal }), 'No lite scan'),
  start: (projectId: string, siteId: string, token: string | null) =>
    moduleRequest<LiteSummary>(path(projectId, siteId), { method: 'POST', body: '{}' }, token),
};

export const liteKey = (projectId: string, siteId: string) => ['lite', projectId, siteId] as const;

export function useLiteScan(projectId: string, siteId: string) {
  return useQuery({
    queryKey: liteKey(projectId, siteId),
    queryFn: ({ signal }) => liteApi.latest(projectId, siteId, signal),
  });
}

export function useStartLiteScan(projectId: string, siteId: string, token: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => liteApi.start(projectId, siteId, token),
    onSettled: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: liteKey(projectId, siteId) }),
        client.invalidateQueries({ queryKey: projectKeys.runs(projectId) }),
        client.invalidateQueries({ queryKey: projectKeys.one(projectId) }),
      ]);
    },
  });
}
