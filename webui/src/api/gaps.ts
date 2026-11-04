/**
 * Gap Analysis module: the project's prioritised findings, drawn from themes, research,
 * monitoring and catalogs. Building a report or recording a decision needs the
 * project's unlock token.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { moduleRequest, orNull } from './research';

export type FindingStatus = 'open' | 'accepted' | 'done' | 'dismissed';

export interface Evidence {
  kind: 'page' | 'pillar' | 'keyword' | 'theme' | 'offering' | 'change' | 'count' | 'competitor';
  label: string;
  website: string | null;
  url: string | null;
  value: number | null;
}

export interface Finding {
  finding_id: string;
  kind: string;
  severity: 'high' | 'medium' | 'low';
  priority: number;
  title: string;
  detail: string;
  action: string;
  theme_id: string | null;
  theme: string | null;
  competitors: string[];
  demand: number;
  evidence: Evidence[];
  status: FindingStatus;
  note: string;
}

export interface GapReport {
  report_id: string;
  project_id: string;
  created_at: string;
  theme_map_id: string | null;
  research_snapshot_id: string | null;
  monitor_check_id: string | null;
  missing_inputs: string[];
  findings: Finding[];
  resolved: string[];
  stats: Record<string, number>;
  settings?: GapSettings;
}

export interface GapSettings {
  min_volume: number;
  competitor_top: number;
}

export interface StatusEntry {
  status: FindingStatus;
  note: string;
  updated_at: string;
}

const enc = encodeURIComponent;
const path = (projectId: string) => `/api/projects/${enc(projectId)}/gaps`;

export const gapsApi = {
  latest: (projectId: string, signal?: AbortSignal) =>
    orNull(moduleRequest<GapReport>(path(projectId), { signal }), 'No gap analysis'),
  build: (projectId: string, token: string | null) =>
    moduleRequest<GapReport>(path(projectId), { method: 'POST', body: '{}' }, token),
  setStatus: (projectId: string, findingId: string, status: FindingStatus, note: string, token: string | null) =>
    moduleRequest<StatusEntry>(
      `${path(projectId)}/findings/${enc(findingId)}/status`,
      { method: 'POST', body: JSON.stringify({ status, note }) },
      token,
    ),
  workbookUrl: (projectId: string) => `${path(projectId)}/workbook`,
  settings: (projectId: string, signal?: AbortSignal) =>
    moduleRequest<GapSettings>(`${path(projectId)}/settings`, { signal }),
  setSettings: (projectId: string, body: GapSettings, token: string | null) =>
    moduleRequest<GapSettings>(`${path(projectId)}/settings`, { method: 'POST', body: JSON.stringify(body) }, token),
};

export const gapKeys = {
  latest: (projectId: string) => ['gaps', projectId] as const,
  settings: (projectId: string) => ['gaps', projectId, 'settings'] as const,
};

export function useGapSettings(projectId: string) {
  return useQuery({
    queryKey: gapKeys.settings(projectId),
    queryFn: ({ signal }) => gapsApi.settings(projectId, signal),
  });
}

export function useSetGapSettings(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { body: GapSettings; token: string | null }) =>
      gapsApi.setSettings(projectId, args.body, args.token),
    onSuccess: (saved) => client.setQueryData(gapKeys.settings(projectId), saved),
  });
}

export function useGaps(projectId: string) {
  return useQuery({ queryKey: gapKeys.latest(projectId), queryFn: ({ signal }) => gapsApi.latest(projectId, signal) });
}

export function useBuildGaps(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (token: string | null) => gapsApi.build(projectId, token),
    onSuccess: (report) => client.setQueryData(gapKeys.latest(projectId), report),
  });
}

export function useSetStatus(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { findingId: string; status: FindingStatus; note: string; token: string | null }) =>
      gapsApi.setStatus(projectId, args.findingId, args.status, args.note, args.token),
    onSuccess: (entry, args) =>
      client.setQueryData<GapReport | null>(gapKeys.latest(projectId), (old) =>
        old
          ? {
              ...old,
              findings: old.findings.map((f) =>
                f.finding_id === args.findingId ? { ...f, status: entry.status, note: entry.note } : f,
              ),
            }
          : old,
      ),
  });
}
