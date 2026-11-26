/**
 * Analytics module: what happens after the click (GA4). A landing page export import is
 * free; a Data API pull needs GA4 switched on, a service account and the project's
 * property id. Importing, pulling and setting the property need the unlock token.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fileBase64, moduleRequest, orNull } from './research';

export interface GroupRollup {
  key: string;
  label: string;
  pages: number;
  sessions: number;
  organic_sessions: number;
  engaged_sessions: number;
  key_events: number;
  revenue: number;
  search_volume: number | null;
}

export interface AnalyticsReport {
  project_id: string;
  website: string;
  built_at: string;
  snapshot_id: string;
  previous_snapshot_id: string | null;
  period: string;
  totals: Record<string, number>;
  channels: Record<string, number>;
  themes: GroupRollup[];
  funnel: GroupRollup[];
  leaky: { url: string; organic_sessions: number; gsc_clicks: number | null; engagement_rate: number; key_events: number; reason: string }[];
  decays: { url: string; metric: string; before: number; after: number; change: number }[];
  cta_gaps: { url: string; sessions: number; ctas: string[]; forms: number }[];
  ai_referrals: { url: string; sessions: number; sources: Record<string, number> }[];
  post_edit: { url: string; change: string; sessions_before: number; sessions_after: number; key_events_before: number; key_events_after: number }[];
  notes: string[];
}

export interface AnalyticsSettings {
  property_id: string | null;
  website: string | null;
}

export interface AnalyticsStatus {
  enabled: boolean;
  credentials_set: boolean;
  pull_refused_because: string | null;
  max_requests: number;
}

export interface Ga4Saved {
  snapshots: { snapshot_id: string; website: string; source: string; start_date: string | null; end_date: string | null; pages: number; sessions: number; notes: string[] }[];
}

const path = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/analytics`;

export const analyticsApi = {
  status: (signal?: AbortSignal) => moduleRequest<AnalyticsStatus>('/api/analytics/status', { signal }),
  report: (projectId: string, signal?: AbortSignal) =>
    orNull(moduleRequest<AnalyticsReport>(path(projectId), { signal }), 'No GA4 data'),
  settings: (projectId: string, signal?: AbortSignal) =>
    moduleRequest<AnalyticsSettings>(`${path(projectId)}/settings`, { signal }),
  saveSettings: (projectId: string, body: AnalyticsSettings, token: string | null) =>
    moduleRequest<AnalyticsSettings>(`${path(projectId)}/settings`, { method: 'POST', body: JSON.stringify(body) }, token),
  importFile: async (projectId: string, file: File, token: string | null) =>
    moduleRequest<Ga4Saved>(
      `${path(projectId)}/import`,
      { method: 'POST', body: JSON.stringify({ file_name: file.name, content_base64: await fileBase64(file) }) },
      token,
    ),
  pull: (projectId: string, token: string | null) =>
    moduleRequest<Ga4Saved>(`${path(projectId)}/pull`, { method: 'POST', body: '{}' }, token),
  workbookUrl: (projectId: string) => `${path(projectId)}/workbook`,
};

export const analyticsKeys = {
  status: ['analytics', 'status'] as const,
  report: (projectId: string) => ['analytics', projectId] as const,
  settings: (projectId: string) => ['analytics', projectId, 'settings'] as const,
};

export function useAnalyticsStatus() {
  return useQuery({ queryKey: analyticsKeys.status, queryFn: ({ signal }) => analyticsApi.status(signal) });
}

export function useAnalytics(projectId: string) {
  return useQuery({ queryKey: analyticsKeys.report(projectId), queryFn: ({ signal }) => analyticsApi.report(projectId, signal) });
}

export function useAnalyticsSettings(projectId: string) {
  return useQuery({ queryKey: analyticsKeys.settings(projectId), queryFn: ({ signal }) => analyticsApi.settings(projectId, signal) });
}

function useRefresh(projectId: string) {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: analyticsKeys.report(projectId) });
    void client.invalidateQueries({ queryKey: ['research', projectId, 'pages'] });
  };
}

export function useImportGa4(projectId: string) {
  const refresh = useRefresh(projectId);
  return useMutation({
    mutationFn: (args: { file: File; token: string | null }) => analyticsApi.importFile(projectId, args.file, args.token),
    onSuccess: refresh,
  });
}

export function usePullGa4(projectId: string) {
  const refresh = useRefresh(projectId);
  return useMutation({ mutationFn: (token: string | null) => analyticsApi.pull(projectId, token), onSuccess: refresh });
}

export function useSaveGa4Settings(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { body: AnalyticsSettings; token: string | null }) => analyticsApi.saveSettings(projectId, args.body, args.token),
    onSuccess: (s: AnalyticsSettings) => client.setQueryData(analyticsKeys.settings(projectId), s),
  });
}
