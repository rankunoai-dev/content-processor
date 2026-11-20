/**
 * Reviewing the engine's verdict on each page of a project scrape: approve it, or reject
 * it with a reason and a next step (scrape again, obtain by hand, exclude from reports).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { moduleRequest } from './research';

export type NextStep = 'scrape_again' | 'obtain_manually' | 'exclude';

export interface VerdictDecision {
  decision: 'approved' | 'rejected' | '';
  next_step: NextStep | '';
  note: string;
  reviewer: string;
  at: string;
}

export interface PageVerdict {
  url: string;
  title: string | null;
  badge: string;
  gaps: string[];
  is_page: boolean;
  note: string;
  decision: VerdictDecision | null;
  history: VerdictDecision[];
}

export interface VerdictList {
  run_id: string;
  site_id: string;
  pages: PageVerdict[];
  counts: Record<string, number>;
}

export interface VerdictIn {
  url: string;
  decision: 'approved' | 'rejected' | '';
  next_step?: NextStep | '';
  note?: string;
  reviewer?: string;
}

const enc = encodeURIComponent;
const path = (p: string, s: string, r: string) =>
  `/api/projects/${enc(p)}/sites/${enc(s)}/runs/${enc(r)}/verdicts`;

export function useVerdicts(projectId: string, siteId: string | null, runId: string | null) {
  return useQuery({
    queryKey: ['verdicts', projectId, siteId, runId] as const,
    queryFn: ({ signal }) => moduleRequest<VerdictList>(path(projectId, siteId ?? '', runId ?? ''), { signal }),
    enabled: siteId !== null && runId !== null,
  });
}

export function useSaveVerdict(projectId: string, siteId: string, runId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { body: VerdictIn; token: string | null }) =>
      moduleRequest<VerdictDecision>(
        path(projectId, siteId, runId),
        { method: 'POST', body: JSON.stringify(args.body) },
        args.token,
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['verdicts', projectId, siteId, runId] }),
  });
}
