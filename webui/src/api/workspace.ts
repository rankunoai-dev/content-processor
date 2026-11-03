/** Each project's status in every module, for the home screen and module pages. */
import { useQuery } from '@tanstack/react-query';
import { ApiError } from './client';

export interface ProjectStatus {
  project_id: string;
  name: string;
  client: string;
  websites: { name: string; role: 'client' | 'competitor'; addresses: string[] }[];
  finished_scrapes: number;
  last_scrape: string | null;
  searchable_scrapes: number;
  themes_built: string | null;
  themes: number;
  last_monitoring_check: string | null;
  alerts: number;
  high_alerts: number;
  research_added: string | null;
  keywords: number;
  gaps_built: string | null;
  findings: number;
  open_findings: number;
  high_findings: number;
}

async function statuses(signal?: AbortSignal): Promise<ProjectStatus[]> {
  let response: Response;
  try {
    response = await fetch('/api/modules/status', { signal, headers: { Accept: 'application/json' } });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Could not reach the app server. Is it still running?');
  }
  if (!response.ok) throw new ApiError(response.status, `The server answered with an error (${response.status}).`);
  return (await response.json()) as ProjectStatus[];
}

export const workspaceKeys = { status: ['modules', 'status'] as const };

export function useModuleStatus() {
  return useQuery({ queryKey: workspaceKeys.status, queryFn: ({ signal }) => statuses(signal) });
}
